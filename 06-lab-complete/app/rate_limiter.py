"""
Rate limiting (tiêu chí 7).

Sliding window 60 giây cho mỗi API key, lưu state trong Redis để chia sẻ
giữa nhiều worker và giữ nguyên state sau khi restart agent.
"""
from collections import deque
import hashlib
import logging
import math
import secrets
import threading
import time

from fastapi import HTTPException, status

from app.config import settings
from app.redis_client import r

logger = logging.getLogger("app.rate_limiter")

WINDOW_SECONDS = 60

# Atomic sliding-window check & record trong Redis sorted set
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest[2] then
        retry_after = math.max(1, math.ceil(tonumber(oldest[2]) + window - now))
    end
    return {0, count, retry_after}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 1)
return {1, count + 1, 0}
"""

_fallback_lock = threading.Lock()
_fallback_windows: dict[str, deque[float]] = {}


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _check_memory_fallback(key_id: str, now: float, limit: int) -> None:
    with _fallback_lock:
        dq = _fallback_windows.setdefault(key_id, deque())
        cutoff = now - WINDOW_SECONDS
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = max(1, math.ceil(dq[0] + WINDOW_SECONDS - now))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        dq.append(now)


def check_rate_limit(key: str) -> None:
    """Kiểm tra giới hạn RATE_LIMIT_PER_MINUTE request / 60s cho mỗi API key."""
    limit = settings.rate_limit_per_minute
    now = time.time()
    key_id = _key_hash(key)
    redis_key = f"ratelimit:{key_id}"

    if r is not None:
        try:
            member = f"{now:.6f}:{secrets.token_hex(6)}"
            allowed, _, retry_after = r.eval(
                _SLIDING_WINDOW_LUA,
                1,
                redis_key,
                f"{now:.6f}",
                str(WINDOW_SECONDS),
                str(limit),
                member,
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                    headers={"Retry-After": str(max(1, int(retry_after)))},
                )
            return
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Redis unavailable for rate limiting, using in-memory fallback",
                extra={"event": "rate_limit_fallback", "error": str(exc)},
            )

    _check_memory_fallback(key_id, now, limit)
