"""对话中的本地文件读写：支持 prompt 块（EdgeFN 兼容）与 OpenAI native tools。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging_setup import log_info
from app.services import file_workspace
from app.services.edgefn_client import complete_chat_turn

log = logging.getLogger("app.file_tools")

CHAT_FILE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "读取受控白名单路径下的文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "用户文件沙箱内的相对路径，如 notes/draft.md",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_document",
            "description": "将正文写入指定路径；format=markdown 时无后缀会自动补 .md。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "content": {"type": "string", "description": "要写入的全文"},
                    "format": {
                        "type": "string",
                        "enum": ["auto", "markdown", "text"],
                        "description": "路径无后缀时的扩展名策略",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "是否覆盖已存在文件，默认 true",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_markdown",
            "description": "将 Markdown 写入文件（便捷封装）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "目标路径"},
                    "content": {"type": "string", "description": "Markdown 正文"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_allowed_write_roots",
            "description": "列出当前服务端允许读写的根目录。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_PROMPT_TOOL_BLOCK = """
## 本地文件工具（必须遵守）

用户要求读/写磁盘文件时，你必须在回复中包含以下格式（JSON 用花括号包裹，content 内换行用 \\n）：

<knowmind_tool_call>
{"name": "write_document", "arguments": {"path": "notes/file.md", "content": "文件正文", "format": "markdown", "overwrite": true}}
</knowmind_tool_call>

可用 name：read_document、write_document、write_markdown、list_allowed_write_roots。
所有路径都必须位于当前用户的文件沙箱内；优先使用相对路径，禁止尝试访问盘符、系统目录或其他用户目录。
写完工具块后停止，等待系统执行；不要假装已经写入成功。
若无需读写文件，正常回答且不要输出该块。
""".strip()

_TOOL_XML_RE = re.compile(
    r"<knowmind_tool_call>\s*(.*?)\s*</knowmind_tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
_WIN_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/])*[^\\/:*?\"<>|\r\n]*",
)
# E盘里的 xxx.md / E盘 xxx.txt
_DISK_FILE_RE = re.compile(
    r"([A-Za-z])\s*盘\s*(?:里|的)?\s*([^\s,，。；;\"'<>|]+?\.(?:md|txt|markdown))",
    re.IGNORECASE,
)
_QUOTED_PATH_RE = re.compile(
    r"[`\"']([^`\"'\r\n]+?\.(?:md|txt|markdown))[`\"']",
    re.IGNORECASE,
)
_REL_PATH_RE = re.compile(
    r"(?<![\w:])(?:[\w\u4e00-\u9fff.-]+[\\/])*[\w\u4e00-\u9fff.-]+\.(?:md|txt|markdown)\b",
    re.IGNORECASE,
)
_READ_KW = ("读取", "读一下", "打开", "查看", "看看", "总结", "概括", "梳理", "提取", "里面", "内容")
_WRITE_KW = (
    "保存",
    "写入",
    "写到",
    "导出",
    "导出到",
    "写进",
    "生成",
    "保存到",
    "写成",
    "存成",
    "存储",
    "markdown",
    "Markdown",
    "文档",
)
_MD_FENCE_RE = re.compile(r"```(?:markdown|md)?\s*([\s\S]*?)```", re.IGNORECASE)


@dataclass
class ToolTraceEntry:
    name: str
    arguments: str
    result: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


@dataclass
class ChatWithToolsResult:
    reasoning: str
    content: str
    tool_traces: list[ToolTraceEntry] = field(default_factory=list)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _calls_from_obj(obj: dict[str, Any]) -> tuple[str, str] | None:
    name = str(obj.get("name") or obj.get("tool") or "").strip()
    if not name:
        return None
    args = obj.get("arguments", obj.get("args", {}))
    if not isinstance(args, dict):
        args = {}
    return name, json.dumps(args, ensure_ascii=False)


def parse_prompt_tool_calls(text: str) -> list[tuple[str, str]]:
    """从模型正文/推理中解析 prompt 模式工具调用。"""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, args: str) -> None:
        key = (name, args)
        if key not in seen:
            seen.add(key)
            out.append(key)

    for m in _TOOL_XML_RE.finditer(text or ""):
        obj = _parse_json_object(m.group(1))
        if obj and (pair := _calls_from_obj(obj)):
            add(*pair)

    for m in _JSON_FENCE_RE.finditer(text or ""):
        obj = _parse_json_object(m.group(1))
        if obj and (pair := _calls_from_obj(obj)):
            add(*pair)

    if "write_document" in (text or "") or "read_document" in (text or ""):
        for m in re.finditer(
            r'\{\s*"name"\s*:\s*"(read_document|write_document|write_markdown|list_allowed_write_roots)"',
            text or "",
        ):
            obj = _parse_json_object(text[m.start() :])
            if obj and (pair := _calls_from_obj(obj)):
                add(*pair)

    return out


def strip_prompt_tool_calls(text: str) -> str:
    cleaned = _TOOL_XML_RE.sub("", text or "")
    return cleaned.strip()


def _combined_model_text(reasoning: str, content: str) -> str:
    parts = [p.strip() for p in (reasoning, content) if p and p.strip()]
    return "\n\n".join(parts)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
    return ""


def all_user_texts(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str):
            continue
        s = c.strip()
        if s and not s.startswith("【系统】"):
            out.append(s)
    return out


def _normalize_path_text(text: str) -> str:
    t = text.replace("＼", "\\").replace("／", "/")
    for disk in "CDEFG":
        t = t.replace(f"{disk}盘", f"{disk}:")
        t = t.replace(f"{disk.lower()}盘", f"{disk}:")
    return t


def extract_path_from_texts(texts: list[str]) -> str | None:
    candidates = expand_path_candidates(texts)
    return candidates[0] if candidates else None


def expand_path_candidates(texts: list[str]) -> list[str]:
    """从用户描述生成候选路径；执行层会强制限制在当前用户沙箱。"""
    found: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        p = p.strip().rstrip(".,;，。；\"'」』)）]")
        if len(p) >= 4 and p not in seen:
            seen.add(p)
            found.append(p)

    for raw in texts:
        if not raw.strip():
            continue
        text = _normalize_path_text(raw)
        for m in _WIN_PATH_RE.finditer(text):
            add(m.group(0))
        for m in _QUOTED_PATH_RE.finditer(text):
            add(m.group(1))
        for m in _REL_PATH_RE.finditer(text):
            add(m.group(0))
        for m in _DISK_FILE_RE.finditer(raw):
            drive, name = m.group(1).upper(), m.group(2).strip()
            add(f"{drive}:\\{name}")
            # 常见：E盘 velochat 目录下的文件
            if "\\" not in name and "/" not in name:
                low = raw.lower()
                if "velochat" in low or "vechat" in low:
                    add(f"{drive}:\\velochat\\{name}")
                    add(f"{drive}:\\VeloChat\\{name}")
                    add(f"{drive}:\\velochat\\聊天记录.md")
                    add(f"{drive}:\\velochat\\项目聊天.md")
    return found


def has_read_intent(user_blob: str) -> bool:
    if not user_blob.strip():
        return False
    if any(k in user_blob for k in _READ_KW):
        return True
    if re.search(r"\b(read|open|summarize|summary)\b", user_blob.lower()):
        return True
    return False


def has_summarize_intent(user_blob: str) -> bool:
    return any(k in user_blob for k in ("总结", "概括", "梳理", "摘要", "提炼", "汇总"))


def build_read_calls(user_texts: list[str]) -> list[tuple[str, str]]:
    user_blob = "\n\n".join(t for t in user_texts if t.strip())
    if not has_read_intent(user_blob):
        return []
    paths = expand_path_candidates(user_texts)
    if not paths:
        log_info("[file_tools] 跳过读取：未识别路径 user=%r", user_blob[:120])
        return []
    # 先尝试最可能的一条；执行层会逐个试
    return [
        ("read_document", json.dumps({"path": p}, ensure_ascii=False))
        for p in paths[:5]
    ]


def build_messages_for_file_task(
    user_request: str,
    file_path: str,
    file_content: str,
    *,
    kb_context: str | None = None,
) -> list[dict[str, str]]:
    """读取本地文件后，让模型总结/分析文件正文。"""
    max_chars = 28_000
    body = file_content[:max_chars]
    if len(file_content) > max_chars:
        body += "\n\n…（后续内容已截断）"
    parts = [
        "你是 KnowMind 助手。下列内容来自用户指定的本地文件，请严格依据文件内容回答；"
        "若要求总结，请用 Markdown 输出结构化摘要（标题、要点列表），勿编造文件中不存在的信息。",
        f"## 文件路径\n`{file_path}`\n\n## 文件正文\n\n{body}",
    ]
    ctx = (kb_context or "").strip()
    if ctx:
        parts.append("## 知识库参考（仅作补充，优先以文件为准）\n\n" + ctx)
    return [
        {"role": "system", "content": "\n\n".join(parts)},
        {"role": "user", "content": user_request.strip()},
    ]


def try_read_files_from_user_texts(
    user_texts: list[str], *, user_id: str = "local"
) -> tuple[list[ToolTraceEntry], str, str | None]:
    """
    按候选路径依次读取，返回 (traces, 合并正文, 成功路径)。
    """
    traces: list[ToolTraceEntry] = []
    for _name, args in build_read_calls(user_texts):
        path = json.loads(args).get("path", "")
        result, ok = _run_tool("read_document", args, user_id=user_id)
        entry = ToolTraceEntry(name="read_document", arguments=args, result=result, ok=ok)
        traces.append(entry)
        if ok and isinstance(result.get("content"), str):
            log_info("[file_tools] 读取成功 path=%s len=%s", path, len(result["content"]))
            return traces, result["content"], str(result.get("path") or path)
        log_info("[file_tools] 读取失败 path=%s err=%s", path, result.get("error"))
    return traces, "", None


def has_write_intent(user_blob: str, assistant_text: str = "") -> bool:
    if not user_blob.strip() and not assistant_text.strip():
        return False
    if any(k in user_blob for k in _WRITE_KW):
        return True
    low = (user_blob + assistant_text).lower()
    if re.search(r"\b(save|write|export)\b", low):
        return True
    if extract_path_from_texts([user_blob]) and re.search(r"\.(md|txt|markdown)\b", user_blob, re.I):
        return True
    if any(k in user_blob for k in ("保存", "写入", "写到", "导出", "存成")) and len(assistant_text) > 150:
        return True
    return False


def _extract_write_content(user_blob: str, assistant_text: str, path: str) -> str | None:
    fence = _MD_FENCE_RE.search(user_blob)
    if fence:
        body = fence.group(1).strip()
        if len(body) >= 2:
            return body

    norm = _normalize_path_text(user_blob)
    norm_path = _normalize_path_text(path)
    idx = norm.find(norm_path)
    if idx >= 0:
        after = norm[idx + len(norm_path) :].strip()
        if after.startswith(("：", ":", "，", ",", "到", "至")):
            after = after[1:].strip()
        if len(after) >= 10:
            return after

    body = strip_prompt_tool_calls(assistant_text).strip()
    skip_prefix = re.compile(
        r"^(好的|当然|以下是|下面是|根据|我已|文件已|成功|正在|请注意)",
        re.IGNORECASE,
    )
    lines = [ln for ln in body.splitlines() if ln.strip() and not skip_prefix.match(ln.strip())]
    body = "\n".join(lines).strip()
    if len(body) >= 8:
        return body
    return None


def build_write_calls(user_texts: list[str], assistant_text: str) -> list[tuple[str, str]]:
    """综合用户多轮消息 + 模型正文，构造 write_document 调用。"""
    user_blob = "\n\n".join(t for t in user_texts if t.strip())
    if not has_write_intent(user_blob, assistant_text):
        log_info("[file_tools] 跳过写入：未识别到保存意图 user=%r", user_blob[:100])
        return []

    path = extract_path_from_texts(user_texts + [assistant_text])
    if not path:
        log_info("[file_tools] 跳过写入：未识别到 Windows 路径 user=%r", user_blob[:100])
        return []

    content = _extract_write_content(user_blob, assistant_text, path)
    if not content:
        log_info("[file_tools] 跳过写入：无正文 path=%s assistant_len=%s", path, len(assistant_text))
        return []

    log_info("[file_tools] 将写入 path=%s content_len=%s", path, len(content))
    return [
        (
            "write_document",
            json.dumps(
                {
                    "path": path,
                    "content": content,
                    "format": "markdown",
                    "overwrite": True,
                },
                ensure_ascii=False,
            ),
        ),
    ]


def infer_write_from_user_message(user_text: str) -> tuple[str, str] | None:
    calls = build_write_calls([user_text], "")
    if not calls:
        return None
    args = json.loads(calls[0][1])
    return str(args.get("path", "")), str(args.get("content", ""))


def infer_write_fallback(user_texts: list[str], assistant_text: str) -> list[tuple[str, str]]:
    return build_write_calls(user_texts, assistant_text)


def inject_prompt_tool_instructions(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [dict(m) for m in messages]
    if not out:
        return [{"role": "system", "content": _PROMPT_TOOL_BLOCK}]
    if out[0].get("role") != "system":
        out.insert(0, {"role": "system", "content": _PROMPT_TOOL_BLOCK})
        return out
    content = str(out[0].get("content") or "")
    if _PROMPT_TOOL_BLOCK not in content:
        out[0] = {**out[0], "content": f"{content}\n\n{_PROMPT_TOOL_BLOCK}".strip()}
    return out


def execute_tool_calls(
    calls: list[tuple[str, str]], *, user_id: str = "local"
) -> list[ToolTraceEntry]:
    entries: list[ToolTraceEntry] = []
    for name, args in calls:
        result, ok = _run_tool(name, args, user_id=user_id)
        entries.append(ToolTraceEntry(name=name, arguments=args, result=result, ok=ok))
    return entries


def try_direct_write_from_user_message(
    user_text: str, *, user_id: str = "local"
) -> ToolTraceEntry | None:
    """用户消息已含路径+正文时，不等待模型，直接写入。"""
    inferred = infer_write_from_user_message(user_text)
    if not inferred:
        return None
    path, content = inferred
    args = json.dumps(
        {"path": path, "content": content, "format": "markdown", "overwrite": True},
        ensure_ascii=False,
    )
    log_info("[file_tools] 直接写入（用户消息）path=%s bytes=%s", path, len(content.encode("utf-8")))
    result, ok = _run_tool("write_document", args, user_id=user_id)
    return ToolTraceEntry(name="write_document", arguments=args, result=result, ok=ok)


def _run_tool(
    name: str, arguments: str, *, user_id: str = "local"
) -> tuple[dict[str, Any], bool]:
    log.info("file_tools execute name=%s args=%s", name, arguments[:500])
    log_info("[file_tools] 执行工具 %s", name)
    raw = file_workspace.execute_tool(
        user_id,
        name,
        arguments,
        max_read_bytes=file_workspace.max_read_bytes(),
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw}
    ok = "error" not in parsed
    if ok:
        log.info("file_tools execute ok name=%s result_keys=%s", name, list(parsed.keys()))
    else:
        log.warning("file_tools execute failed name=%s err=%s", name, parsed.get("error"))
    return parsed, ok


def _use_native_tools() -> bool:
    return settings.file_tools_mode.strip().lower() == "native"


async def _execute_calls(
    calls: list[tuple[str, str]],
    *,
    traces: list[ToolTraceEntry],
    on_tool: Callable[[ToolTraceEntry], Awaitable[None]] | None,
    user_id: str,
) -> list[str]:
    result_lines: list[str] = []
    for name, args in calls:
        result, ok = _run_tool(name, args, user_id=user_id)
        entry = ToolTraceEntry(name=name, arguments=args, result=result, ok=ok)
        traces.append(entry)
        if on_tool is not None:
            await on_tool(entry)
        result_lines.append(f"- {name}: {json.dumps(result, ensure_ascii=False)}")
    return result_lines


async def _complete_native(
    messages: list[dict[str, Any]],
    *,
    on_tool: Callable[[ToolTraceEntry], Awaitable[None]] | None,
    user_id: str,
) -> ChatWithToolsResult:
    working = list(messages)
    traces: list[ToolTraceEntry] = []
    last_reasoning = ""
    last_content = ""
    max_rounds = settings.file_tools_max_rounds

    for _ in range(max_rounds):
        turn = await complete_chat_turn(working, tools=CHAT_FILE_TOOL_DEFINITIONS)
        last_reasoning = turn.reasoning or last_reasoning
        last_content = turn.content or last_content

        if not turn.tool_calls:
            return ChatWithToolsResult(
                reasoning=last_reasoning,
                content=last_content,
                tool_traces=traces,
            )

        working.append(
            {
                "role": "assistant",
                "content": turn.content or None,
                "tool_calls": turn.tool_calls,
            },
        )

        for tc in turn.tool_calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            args = str(fn.get("arguments") or "{}")
            result, ok = _run_tool(name, args, user_id=user_id)
            entry = ToolTraceEntry(name=name, arguments=args, result=result, ok=ok)
            traces.append(entry)
            if on_tool is not None:
                await on_tool(entry)
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                },
            )

    return ChatWithToolsResult(
        reasoning=last_reasoning,
        content=last_content or "（已达文件工具调用轮次上限，请重试或简化请求）",
        tool_traces=traces,
    )


async def _complete_prompt(
    messages: list[dict[str, Any]],
    *,
    on_tool: Callable[[ToolTraceEntry], Awaitable[None]] | None,
    user_id: str,
) -> ChatWithToolsResult:
    working = inject_prompt_tool_instructions(messages)
    traces: list[ToolTraceEntry] = []
    last_reasoning = ""
    last_content = ""
    user_texts = all_user_texts(messages)
    max_rounds = settings.file_tools_max_rounds
    fallback_used = False

    for round_idx in range(max_rounds):
        turn = await complete_chat_turn(working)
        last_reasoning = turn.reasoning or last_reasoning
        combined = _combined_model_text(turn.reasoning, turn.content)
        calls = parse_prompt_tool_calls(combined)

        log.info(
            "file_tools prompt round=%s content_len=%s reasoning_len=%s parsed_calls=%s",
            round_idx,
            len(turn.content or ""),
            len(turn.reasoning or ""),
            len(calls),
        )
        log_info(
            "[file_tools] round=%s parsed=%s user_last=%r",
            round_idx,
            len(calls),
            (_last_user_text(messages) or "")[:80],
        )

        if not calls and round_idx == 0 and not fallback_used:
            fallback = infer_write_fallback(user_texts, combined)
            if fallback:
                calls = fallback
                fallback_used = True
                log.info("file_tools using user/assistant fallback write calls=%s", len(calls))
                log_info("[file_tools] 启用备用写入 calls=%s", len(calls))

        if not calls:
            last_content = strip_prompt_tool_calls(turn.content or "") or (turn.content or "")
            if not last_content.strip() and turn.reasoning:
                last_content = strip_prompt_tool_calls(turn.reasoning)
            return ChatWithToolsResult(
                reasoning=last_reasoning,
                content=last_content,
                tool_traces=traces,
            )

        working.append({"role": "assistant", "content": combined})
        result_lines = await _execute_calls(
            calls, traces=traces, on_tool=on_tool, user_id=user_id
        )
        working.append(
            {
                "role": "user",
                "content": "【系统】工具已执行，结果如下。请用简短自然语言回复用户，勿再输出 knowmind_tool_call 块除非仍需继续读写：\n"
                + "\n".join(result_lines),
            },
        )

    return ChatWithToolsResult(
        reasoning=last_reasoning,
        content=last_content or "（已达文件工具调用轮次上限，请重试或简化请求）",
        tool_traces=traces,
    )


async def complete_chat_with_file_tools(
    messages: list[dict[str, Any]],
    *,
    on_tool: Callable[[ToolTraceEntry], Awaitable[None]] | None = None,
    user_id: str = "local",
) -> ChatWithToolsResult:
    """
    多轮调用模型直至不再请求工具或达到轮次上限。
    默认 prompt 模式，兼容未开启 vLLM auto tool choice 的 EdgeFN。
    """
    log.info("file_tools start mode=%s", settings.file_tools_mode)
    log_info("[file_tools] 开始 file_tools_mode=%s", settings.file_tools_mode)
    if _use_native_tools():
        try:
            return await _complete_native(messages, on_tool=on_tool, user_id=user_id)
        except RuntimeError as e:
            msg = str(e).lower()
            if "tool" in msg and ("choice" in msg or "400" in msg):
                log.warning("file_tools native failed, fallback to prompt: %s", e)
                return await _complete_prompt(messages, on_tool=on_tool, user_id=user_id)
            raise
    return await _complete_prompt(messages, on_tool=on_tool, user_id=user_id)
