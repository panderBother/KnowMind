"""对外 HTTP 请求：可选是否信任系统代理、瞬时网络错误重试。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)

_RETRYABLE = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
    httpx.NetworkError,
)

T = TypeVar("T")


def _retry_delay(attempt: int) -> float:
    return min(2.5, 0.35 * (2**attempt))


def sync_request_with_retry(
    fn: Callable[[httpx.Client], httpx.Response],
    *,
    timeout: httpx.Timeout | None = None,
) -> httpx.Response:
    """在可重试的网络错误时自动重试（同步）。"""
    settings = get_settings()
    last: Exception | None = None
    for attempt in range(max(1, settings.http_max_retries)):
        try:
            with httpx.Client(timeout=timeout, trust_env=settings.http_trust_env) as client:
                return fn(client)
        except _RETRYABLE as e:
            last = e
            if attempt + 1 >= settings.http_max_retries:
                break
            log.warning("HTTP 请求失败，%ss 后重试 (%s/%s): %s", _retry_delay(attempt), attempt + 2, settings.http_max_retries, e)
            time.sleep(_retry_delay(attempt))
    assert last is not None
    raise last


async def async_request_with_retry(
    fn: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
    *,
    timeout: httpx.Timeout | None = None,
) -> httpx.Response:
    """在可重试的网络错误时自动重试（异步）。"""
    settings = get_settings()
    last: Exception | None = None
    for attempt in range(max(1, settings.http_max_retries)):
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=settings.http_trust_env) as client:
                return await fn(client)
        except _RETRYABLE as e:
            last = e
            if attempt + 1 >= settings.http_max_retries:
                break
            log.warning(
                "HTTP 异步请求失败，%ss 后重试 (%s/%s): %s",
                _retry_delay(attempt),
                attempt + 2,
                settings.http_max_retries,
                e,
            )
            await asyncio.sleep(_retry_delay(attempt))
    assert last is not None
    raise last


def friendly_connect_error(exc: BaseException) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    if "UNEXPECTED_EOF" in msg or "SSL" in msg.upper():
        return (
            "连接嵌入/对话 API 时 SSL 失败（常见于系统代理 Clash/VPN 干扰）。"
            "请在 knowmind-server/.env 设置 HTTP_TRUST_ENV=false 后重启服务，或检查网络与 API 密钥。"
        )
    if "ConnectError" in exc.__class__.__name__ or "connect" in msg.lower():
        return f"无法连接外部 API：{msg}。请检查网络、代理设置（HTTP_TRUST_ENV）与 EDGEFN/嵌入服务配置。"
    return f"外部 API 请求失败：{msg}"
