"""出站 URL 安全校验，阻止 SSRF 访问本机、内网和保留地址。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _validate_ip(ip_text: str) -> None:
    ip = ipaddress.ip_address(ip_text)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError("目标地址属于本机、内网或保留网段，已拒绝访问")


def validate_public_http_url_syntax(url: str) -> str:
    """同步检查 URL 结构和显式 IP；DNS 解析由异步检查完成。"""
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("仅支持有效的 http/https 公网 URL")
    if parsed.username or parsed.password:
        raise ValueError("URL 不允许包含用户名或密码")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError("不允许访问本机或内部域名")
    try:
        _validate_ip(host)
    except ValueError as exc:
        if "does not appear" not in str(exc):
            raise
    return value


async def validate_public_http_url(url: str) -> str:
    value = validate_public_http_url_syntax(url)
    parsed = urlparse(value)
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
        return value
    except ValueError:
        pass

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("目标域名无法解析") from exc
    addresses = {str(info[4][0]).split("%", 1)[0] for info in infos}
    if not addresses:
        raise ValueError("目标域名没有可用地址")
    for address in addresses:
        _validate_ip(address)
    return value
