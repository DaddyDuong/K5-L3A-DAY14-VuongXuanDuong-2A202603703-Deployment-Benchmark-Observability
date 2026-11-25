"""
Cost guard (tiêu chí 8).

Cộng dồn chi phí LLM theo token vào Redis và chặn bằng HTTP 402 khi vượt
DAILY_BUDGET_USD hoặc MONTHLY_BUDGET_USD.
"""
from datetime import datetime, timezone
import logging
import threading

from fastapi import HTTPException, status

from app.config import settings
from app.redis_client import r

logger = logging.getLogger("app.cost_guard")

# Giá USD / 1K tokens (tham khảo gpt-4o-mini)
PRICE_INPUT_PER_1K = 0.00015
PRICE_OUTPUT_PER_1K = 0.0006

DAILY_TTL_SECONDS = 48 * 3600
MONTHLY_TTL_SECONDS = 35 * 24 * 3600

_fallback_lock = threading.Lock()
_fallback_spent: dict[str, float] = {}


def _budget_keys() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (
        f"cost:daily:{now.strftime('%Y-%m-%d')}",
        f"cost:monthly:{now.strftime('%Y-%m')}",
    )


def estimate_tokens(text: str) -> int:
    """Ước lượng số token từ độ dài văn bản (~4 ký tự / token)."""
    return max(1, (len(text) + 3) // 4)


def _get_spent(daily_key: str, monthly_key: str) -> tuple[float, float]:
    if r is not None:
        try:
            vals = r.mget([daily_key, monthly_key])
            daily = float(vals[0]) if vals[0] is not None else 0.0
            monthly = float(vals[1]) if vals[1] is not None else 0.0
            return daily, monthly
        except Exception as exc:
            logger.warning(
                "Redis unavailable for budget check, using in-memory fallback",
                extra={"event": "cost_guard_fallback", "error": str(exc)},
            )
    with _fallback_lock:
        return _fallback_spent.get(daily_key, 0.0), _fallback_spent.get(monthly_key, 0.0)


def check_budget() -> None:
    """Gọi TRƯỚC khi gọi LLM. Hết budget -> raise HTTPException(status_code=402)."""
    daily_key, monthly_key = _budget_keys()
    daily_spent, monthly_spent = _get_spent(daily_key, monthly_key)

    if daily_spent >= settings.daily_budget_usd:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Daily budget limit exceeded",
        )
    if monthly_spent >= settings.monthly_budget_usd:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Monthly budget limit exceeded",
        )


def record_cost(input_tokens: int, output_tokens: int) -> float:
    """Gọi SAU khi gọi LLM. Cộng chi phí của request vào Redis, trả về chi phí request."""
    cost = (
        (input_tokens / 1000.0) * PRICE_INPUT_PER_1K
        + (output_tokens / 1000.0) * PRICE_OUTPUT_PER_1K
    )
    daily_key, monthly_key = _budget_keys()

    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.incrbyfloat(daily_key, cost)
            pipe.expire(daily_key, DAILY_TTL_SECONDS)
            pipe.incrbyfloat(monthly_key, cost)
            pipe.expire(monthly_key, MONTHLY_TTL_SECONDS)
            pipe.execute()
            return cost
        except Exception as exc:
            logger.warning(
                "Redis unavailable for recording cost, using in-memory fallback",
                extra={"event": "cost_record_fallback", "error": str(exc)},
            )

    with _fallback_lock:
        _fallback_spent[daily_key] = _fallback_spent.get(daily_key, 0.0) + cost
        _fallback_spent[monthly_key] = _fallback_spent.get(monthly_key, 0.0) + cost
    return cost
