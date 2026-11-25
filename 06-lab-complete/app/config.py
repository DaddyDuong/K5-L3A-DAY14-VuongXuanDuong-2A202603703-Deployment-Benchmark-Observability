"""
Config — 12-Factor: mọi cấu hình đọc từ biến môi trường.
"""
from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    port: int = 8000
    environment: str = "development"
    log_level: str = "INFO"
    agent_api_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_per_minute: int = 10
    daily_budget_usd: float = 1.0
    monthly_budget_usd: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            port=_get_int("PORT", 8000),
            environment=os.getenv("ENVIRONMENT", "development"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            agent_api_key=os.getenv("AGENT_API_KEY", ""),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            rate_limit_per_minute=max(1, _get_int("RATE_LIMIT_PER_MINUTE", 10)),
            daily_budget_usd=_get_float("DAILY_BUDGET_USD", 1.0),
            monthly_budget_usd=_get_float("MONTHLY_BUDGET_USD", 30.0),
        )


settings = Settings.from_env()
