"""Redis 优先、内存降级的固定窗口限流与用户额度。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.core.config import settings

log = logging.getLogger(__name__)


@dataclass
class _Bucket:
    count: int
    expires_at: float


_buckets: dict[str, _Bucket] = {}
_lock = asyncio.Lock()
_redis_disabled_until = 0.0


async def _consume_memory(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.time()
    async with _lock:
        bucket = _buckets.get(key)
        if bucket is None or bucket.expires_at <= now:
            bucket = _Bucket(count=0, expires_at=now + window_seconds)
            _buckets[key] = bucket
        bucket.count += 1
        if len(_buckets) > 10_000:
            expired = [k for k, value in _buckets.items() if value.expires_at <= now]
            for old_key in expired:
                _buckets.pop(old_key, None)
        return bucket.count <= limit, max(1, int(bucket.expires_at - now))


async def _consume_redis(key: str, limit: int, window_seconds: int) -> tuple[bool, int] | None:
    global _redis_disabled_until
    now = time.time()
    if now < _redis_disabled_until:
        return None
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            count = int(await client.incr(key))
            if count == 1:
                await client.expire(key, window_seconds)
            ttl = int(await client.ttl(key))
            return count <= limit, max(1, ttl if ttl > 0 else window_seconds)
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        _redis_disabled_until = now + 30
        log.warning("rate limiter redis unavailable; using memory fallback: %s", exc)
        return None


async def consume(key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    if limit <= 0:
        return True, 0
    bucket = int(time.time() // window_seconds)
    namespaced = f"knowmind:limit:{key}:{bucket}"
    result = await _consume_redis(namespaced, limit, window_seconds)
    if result is not None:
        return result
    return await _consume_memory(namespaced, limit, window_seconds)


def request_ip(request: Request) -> str:
    if settings.rate_limit_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


async def enforce(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    message: str,
) -> None:
    allowed, retry_after = await consume(key, limit=limit, window_seconds=window_seconds)
    if allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=message,
        headers={"Retry-After": str(retry_after)},
    )


async def enforce_auth_rate(request: Request) -> None:
    await enforce(
        f"auth:{request_ip(request)}",
        limit=settings.auth_rate_limit_per_minute,
        window_seconds=60,
        message="登录或注册请求过于频繁，请稍后重试",
    )


async def enforce_chat_limits(user_id: str) -> None:
    await enforce(
        f"chat-minute:{user_id}",
        limit=settings.chat_rate_limit_per_minute,
        window_seconds=60,
        message="对话请求过于频繁，请稍后重试",
    )
    await enforce(
        f"chat-day:{user_id}",
        limit=settings.chat_daily_quota,
        window_seconds=86_400,
        message="今日对话额度已用完",
    )


async def enforce_upload_limits(user_id: str) -> None:
    await enforce(
        f"upload-minute:{user_id}",
        limit=settings.upload_rate_limit_per_minute,
        window_seconds=60,
        message="上传请求过于频繁，请稍后重试",
    )
    await enforce(
        f"upload-day:{user_id}",
        limit=settings.upload_daily_quota,
        window_seconds=86_400,
        message="今日上传额度已用完",
    )
