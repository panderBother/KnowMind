"""外部 MCP 客户端：远程 URL（SSE / Streamable HTTP）与本地 stdio（command）。"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from app.core.config import settings
from app.utils.outbound_security import validate_public_http_url
from app.services import mcp_registry

log = logging.getLogger(__name__)

SEP = "::"


def _format_connect_error(err: Exception) -> str:
    """把 TaskGroup / HTTPStatusError 等包装异常转成可读信息。"""
    seen: set[int] = set()

    def walk(e: BaseException) -> str | None:
        oid = id(e)
        if oid in seen:
            return None
        seen.add(oid)
        import httpx

        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code
            if status == 401:
                return "401 Unauthorized（请在 MCP 配置的 HTTP 请求头中填写 Authorization: Bearer 你的BizyAir_API_Key）"
            if status == 403:
                return "403 Forbidden（API Key 无效或无权访问该 MCP）"
            return f"HTTP {status} {e.response.reason_phrase}"
        if isinstance(e, httpx.ReadTimeout):
            return "ReadTimeout（远程 MCP 响应超时，请稍后重试或检查网络/API 服务状态）"
        if isinstance(e, httpx.ConnectTimeout):
            return "ConnectTimeout（无法连接远程 MCP，请检查 URL 与网络）"
        msg = str(e).strip()
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                detail = walk(sub)
                if detail:
                    return detail
        cause = e.__cause__
        if isinstance(cause, BaseException):
            detail = walk(cause)
            if detail:
                return detail
        return msg or type(e).__name__

    return walk(err) or type(err).__name__


def _should_fallback_to_sse(err: Exception) -> bool:
    """ReadTimeout 时不再尝试 SSE，避免双倍等待。"""
    import httpx

    if isinstance(err, httpx.ReadTimeout):
        return False
    return True


@dataclass
class McpUrlBinding:
    custom_id: str
    display_name: str
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class McpToolBinding:
    custom_id: str
    server_name: str
    tool_name: str
    openai_name: str
    description: str
    parameters: dict[str, Any]


def list_enabled_url_bindings(user_id: str) -> list[McpUrlBinding]:
    """已启用的外部 MCP；公网默认仅返回 URL 型配置。"""
    out: list[McpUrlBinding] = []
    for c in mcp_registry.list_tools(user_id).custom:
        if not c.enabled:
            continue
        url = (c.config.url or "").strip()
        command = (c.config.command or "").strip()
        if not url and not command:
            continue
        if command and not settings.external_mcp_stdio_enabled:
            continue
        if url:
            hdrs = dict(c.config.headers or {})
            for k, v in (c.config.env or {}).items():
                if str(k).startswith("HEADER_"):
                    hdrs[str(k)[len("HEADER_") :]] = str(v)
            out.append(
                McpUrlBinding(
                    custom_id=c.id,
                    display_name=c.name,
                    url=url,
                    headers=hdrs,
                ),
            )
        else:
            env = {str(k): str(v) for k, v in (c.config.env or {}).items() if not str(k).startswith("HEADER_")}
            cwd = (c.config.cwd or "").strip() or None
            out.append(
                McpUrlBinding(
                    custom_id=c.id,
                    display_name=c.name,
                    command=command,
                    args=[str(a) for a in (c.config.args or [])],
                    env=env,
                    cwd=cwd,
                ),
            )
    return out


def qualified_tool_name(custom_id: str, tool_name: str) -> str:
    return f"{custom_id}{SEP}{tool_name}"


_MEDIA_URL_RE = re.compile(r'https?://[^\s<>"\]\)]+', re.IGNORECASE)


def mcp_tool_result_ok(result: dict[str, Any]) -> bool:
    """BizyAir 等 MCP 常返回 {\"error\": null, ...}，不能用 \"error\" in result 判断失败。"""
    if bool(result.get("isError")):
        return False
    err = result.get("error")
    if isinstance(err, str) and err.strip():
        return False
    return True


def _classify_media_url(url: str) -> str | None:
    u = url.strip()
    if u.startswith("data:image/"):
        return "image"
    if u.startswith("data:video/"):
        return "video"
    low = u.lower()
    if re.search(r"\.(?:png|jpe?g|webp|gif|bmp|svg|avif|heic)(?:$|[?#])", low):
        return "image"
    if re.search(r"\.(?:mp4|webm|mov|mkv|avi|m3u8|ogv)(?:$|[?#])", low):
        return "video"
    return None


def extract_media_urls_from_value(value: Any, out: list[str]) -> None:
    seen = set(out)
    if value is None:
        return
    if isinstance(value, str):
        for match in _MEDIA_URL_RE.finditer(value):
            url = match.group(0).rstrip(".,;:!?)")
            if url not in seen and _classify_media_url(url):
                seen.add(url)
                out.append(url)
        if value.startswith("data:image/") or value.startswith("data:video/"):
            if value not in seen:
                seen.add(value)
                out.append(value)
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            key_low = str(key).lower()
            if key_low in {"url", "uri", "href", "link", "image_url", "image", "video_url", "video", "src"}:
                if isinstance(nested, str):
                    if nested.startswith("data:") or _classify_media_url(nested):
                        if nested not in seen:
                            seen.add(nested)
                            out.append(nested)
            extract_media_urls_from_value(nested, out)
        return
    if isinstance(value, list):
        for item in value:
            extract_media_urls_from_value(item, out)


def enrich_mcp_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """从 MCP 原始 JSON / 文本中归一化 media_urls，供 SSE 与前端画廊使用。"""
    media_urls: list[str] = []
    extract_media_urls_from_value(result, media_urls)
    if not media_urls:
        return result
    enriched = dict(result)
    enriched["media_urls"] = media_urls
    return enriched


def _content_block_to_parts(block: Any) -> tuple[list[str], list[str]]:
    """解析 MCP content block，返回 (text_parts, media_urls)。"""
    texts: list[str] = []
    media: list[str] = []

    text = getattr(block, "text", None)
    if isinstance(text, str) and text.strip():
        texts.append(text)
        extract_media_urls_from_value(text, media)
        return texts, media

    block_type = getattr(block, "type", None)
    if block_type == "image":
        data = getattr(block, "data", None)
        mime = str(getattr(block, "mimeType", None) or "image/png")
        if isinstance(data, str) and data.strip():
            media.append(f"data:{mime};base64,{data.strip()}")
        return texts, media

    if block_type == "resource_link":
        uri = getattr(block, "uri", None)
        if uri is not None:
            uri_s = str(uri)
            if _classify_media_url(uri_s):
                media.append(uri_s)
            else:
                texts.append(uri_s)
        return texts, media

    uri = getattr(block, "uri", None)
    if uri is not None:
        uri_s = str(uri)
        if _classify_media_url(uri_s):
            media.append(uri_s)
        else:
            texts.append(uri_s)
        return texts, media

    data = getattr(block, "data", None)
    if isinstance(data, str) and data.strip():
        mime = str(getattr(block, "mimeType", None) or "application/octet-stream")
        if mime.startswith("image/") or mime.startswith("video/"):
            media.append(f"data:{mime};base64,{data.strip()}")
        else:
            try:
                decoded = base64.b64decode(data.strip(), validate=False)
                texts.append(decoded.decode("utf-8", errors="replace"))
            except Exception:
                texts.append(data)
        return texts, media

    blob = str(block).strip()
    if blob:
        texts.append(blob)
        extract_media_urls_from_value(blob, media)
    return texts, media


def parse_qualified_tool_name(qualified: str) -> tuple[str, str] | None:
    if SEP not in qualified:
        return None
    cid, _, tname = qualified.partition(SEP)
    if not cid or not tname:
        return None
    return cid, tname


@asynccontextmanager
async def _stdio_session(binding: McpUrlBinding) -> AsyncIterator[ClientSession]:
    if not settings.external_mcp_stdio_enabled:
        raise RuntimeError("服务端已禁用 command/stdio MCP")
    merged_env = {**os.environ, **binding.env} if binding.env else None
    params = StdioServerParameters(
        command=str(binding.command),
        args=list(binding.args or []),
        env=merged_env,
        cwd=binding.cwd,
    )
    async with stdio_client(params) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def _url_session(binding: McpUrlBinding) -> AsyncIterator[ClientSession]:
    url = (binding.url or "").strip()
    if not url:
        raise RuntimeError("MCP 配置缺少 url")
    if urlparse(url).scheme.lower() != "https":
        raise RuntimeError("外部 MCP 仅允许公网 HTTPS URL")
    await validate_public_http_url(url)
    timeout = float(settings.external_mcp_connect_timeout)
    read_timeout = float(settings.external_mcp_read_timeout)
    headers = binding.headers or None
    last_err: Exception | None = None

    try:
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=read_timeout,
        ) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
                return
    except Exception as e:
        last_err = e
        log.warning(
            "MCP streamable_http connect failed url=%s: %s",
            url,
            _format_connect_error(e),
        )
        if not _should_fallback_to_sse(e):
            raise RuntimeError(f"无法连接 MCP 服务 {url}: {_format_connect_error(e)}")

    try:
        async with sse_client(
            url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=read_timeout,
        ) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
                return
    except Exception as e:
        last_err = e
        log.warning(
            "MCP sse connect failed url=%s: %s",
            url,
            _format_connect_error(e),
        )

    raise RuntimeError(f"无法连接 MCP 服务 {url}: {_format_connect_error(last_err) if last_err else 'unknown'}")


@asynccontextmanager
async def _open_session(binding: McpUrlBinding) -> AsyncIterator[ClientSession]:
    if binding.command:
        async with _stdio_session(binding) as session:
            yield session
        return
    async with _url_session(binding) as session:
        yield session


async def discover_tools(binding: McpUrlBinding) -> list[McpToolBinding]:
    async with _open_session(binding) as session:
        listed = await session.list_tools()
    tools = listed.tools if listed else []
    out: list[McpToolBinding] = []
    for t in tools:
        name = str(getattr(t, "name", "") or "")
        if not name:
            continue
        schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump()
        elif not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        out.append(
            McpToolBinding(
                custom_id=binding.custom_id,
                server_name=binding.display_name,
                tool_name=name,
                openai_name=qualified_tool_name(binding.custom_id, name),
                description=(getattr(t, "description", None) or f"{binding.display_name} / {name}")[:2000],
                parameters=schema,
            ),
        )
    return out


async def discover_all_tools(user_id: str) -> tuple[list[McpToolBinding], list[str]]:
    """返回 (工具列表, 各 server 发现失败信息)。"""
    bindings = list_enabled_url_bindings(user_id)
    all_tools: list[McpToolBinding] = []
    errors: list[str] = []
    for b in bindings:
        try:
            found = await discover_tools(b)
            all_tools.extend(found)
            log.info("mcp discover ok server=%s tools=%s", b.display_name, len(found))
        except Exception as e:
            msg = f"{b.display_name}: {e!s}"
            errors.append(msg)
            log.warning("mcp discover failed %s", msg)
    return all_tools, errors


def bindings_to_openai_tools(bindings: list[McpToolBinding]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": b.openai_name,
                "description": b.description,
                "parameters": b.parameters,
            },
        }
        for b in bindings
    ]


async def call_tool(binding: McpUrlBinding, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with _open_session(binding) as session:
        result = await session.call_tool(tool_name, arguments=arguments)
    content_parts: list[str] = []
    media_urls: list[str] = []
    if result.content:
        for block in result.content:
            texts, media = _content_block_to_parts(block)
            content_parts.extend(texts)
            for url in media:
                if url not in media_urls:
                    media_urls.append(url)
    text = "\n".join(content_parts).strip()
    structured = getattr(result, "structuredContent", None)
    is_error = bool(getattr(result, "isError", False))
    parsed: dict[str, Any]
    if structured is not None:
        parsed = {"text": text, "structured": structured, "isError": is_error}
    elif text:
        try:
            loaded = json.loads(text)
            parsed = loaded if isinstance(loaded, dict) else {"text": text, "isError": is_error}
        except json.JSONDecodeError:
            parsed = {"text": text, "isError": is_error}
    else:
        parsed = {"isError": is_error}
    if media_urls:
        existing = parsed.get("media_urls")
        merged = list(media_urls)
        if isinstance(existing, list):
            for url in existing:
                if isinstance(url, str) and url not in merged:
                    merged.append(url)
        parsed["media_urls"] = merged
    return enrich_mcp_tool_result(parsed)


def binding_by_id(user_id: str) -> dict[str, McpUrlBinding]:
    return {b.custom_id: b for b in list_enabled_url_bindings(user_id)}
