"""
Production AI Agent — Part 6 Final Project.

Đạt đủ 12 tiêu chí production-ready:
  - /health (liveness) & /ready (readiness kiểm tra Redis)
  - API Key authentication (X-API-Key -> 401)
  - Redis-backed sliding-window rate limiting (-> 429 + Retry-After)
  - Redis-backed daily & monthly cost guard (-> 402)
  - 12-Factor config từ biến môi trường
  - Structured JSON logging
  - Graceful shutdown qua FastAPI lifespan (SIGTERM hoàn thành request đang chạy)
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import sys
import time
from typing import Optional
import uuid

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, estimate_tokens, record_cost
from app.rate_limiter import check_rate_limit
from app.redis_client import close_redis, redis_healthy
from utils.mock_llm import ask as llm_ask

_STANDARD_LOG_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    """Format mỗi dòng log của app thành đúng 1 JSON object hợp lệ."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "env": settings.environment,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(payload, ensure_ascii=False, default=str)


def _configure_logging() -> logging.Logger:
    level = getattr(logging, settings.log_level, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    app_logger = logging.getLogger("app")
    app_logger.handlers.clear()
    app_logger.addHandler(handler)
    app_logger.setLevel(level)
    app_logger.propagate = False
    return app_logger


logger = _configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Agent starting up",
        extra={
            "event": "startup",
            "environment": settings.environment,
            "port": settings.port,
            "redis_configured": bool(settings.redis_url),
        },
    )
    try:
        yield
    finally:
        logger.info("Agent shutting down gracefully", extra={"event": "shutdown"})
        close_redis()


app = FastAPI(title="Production AI Agent", lifespan=lifespan)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.error(
            "Unhandled error while processing request",
            extra={
                "event": "request_error",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": 500,
                "ms": elapsed_ms,
                "error": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_server_error", "detail": "An unexpected error occurred"},
        )

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "HTTP request completed",
        extra={
            "event": "request",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": elapsed_ms,
        },
    )
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "http_error", "detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [
        {"loc": list(err.get("loc", [])), "msg": err.get("msg", "Invalid input")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "validation_error", "detail": errors},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception",
        extra={
            "event": "unhandled_exception",
            "path": request.url.path,
            "error": type(exc).__name__,
        },
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_server_error", "detail": "An unexpected error occurred"},
    )


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: Optional[str] = Field(default=None, max_length=128)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("question must not be empty")
        return cleaned


@app.get("/health")
def health():
    """Liveness probe: luôn trả 200 khi process còn chạy, kể cả khi Redis down."""
    return {"status": "healthy", "environment": settings.environment}


@app.get("/ready")
def ready():
    """Readiness probe: 200 khi Redis sẵn sàng, 503 khi Redis không kết nối được."""
    if redis_healthy():
        return {"status": "ready", "redis": "connected"}
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "redis": "disconnected"},
    )


@app.post("/ask")
def ask(body: AskRequest, api_key: str = Depends(verify_api_key)):
    check_rate_limit(api_key)
    check_budget()

    answer = llm_ask(body.question)

    input_tokens = estimate_tokens(body.question)
    output_tokens = estimate_tokens(answer)
    cost_usd = record_cost(input_tokens, output_tokens)

    logger.info(
        "Answered question",
        extra={
            "event": "ask_completed",
            "user_id": body.user_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost_usd, 8),
        },
    )
    return {
        "question": body.question,
        "answer": answer,
        "user_id": body.user_id,
        "cost_usd": round(cost_usd, 6),
    }
