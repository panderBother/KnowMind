import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.memory_constants import approx_token_count
from app.indexing.chat_memory_factory import get_chat_memory_index
from app.ingest.embedding import embed_texts
from app.models.orm import ChatMessage, Conversation
from app.models.schemas import ChatRequest, ChatResponse
from app.services.conversation_service import (
    append_message,
    load_messages_ordered,
    load_summaries_concat,
    load_working_memory_concat,
    resolve_conversation,
    rows_for_redis,
)
from app.core.logging_setup import log_info
from app.services.chat_file_tools import (
    ToolTraceEntry,
    all_user_texts,
    build_messages_for_file_task,
    build_write_calls,
    complete_chat_with_file_tools,
    execute_tool_calls,
    has_read_intent,
    has_write_intent,
    try_direct_write_from_user_message,
    try_read_files_from_user_texts,
)
from app.services.edgefn_client import complete_chat_turn
from app.services.file_tool_logging import tool_log_message
from app.services.edgefn_client import (
    build_chat_messages,
    build_chat_messages_multi,
    complete_chat,
    iter_chat_stream as iter_edgefn_token_stream,
    iter_text_chunks,
)
from app.services.memory_prompt import (
    apply_recent_message_window,
    format_retrieval_hits_markdown,
    history_pairs_from_messages,
    retrieval_query_text,
)
from app.services.llm_planner import (
    apply_plan_to_request,
    build_llm_execution_plan,
    planner_sse,
)
from app.services.chat_prefetch import (
    PrefetchResult,
    fetch_arxiv_md,
    fetch_semantic_scholar_md,
    merge_context_parts,
    want_arxiv,
    want_semantic_scholar,
    yield_prefetch_steps,
)
from app.services.redis_conv import set_recent_messages

T = TypeVar("T")


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _agent_step_sse(
    step: str,
    *,
    status: str = "done",
    detail: str = "",
    meta: dict | None = None,
) -> dict:
    payload: dict = {"type": "agent_step", "step": step, "status": status}
    if detail:
        payload["detail"] = detail
    if meta:
        payload["meta"] = meta
    return payload


async def _sse_rag_sources(
    session: AsyncSession | None,
    kb_id: str | None,
    hits: list,
) -> dict | None:
    if not kb_id or not hits:
        return None
    from app.core.config import get_settings
    from app.services.rag_citations import build_rag_sources_payload
    from app.services.rag_logging_service import filter_confident_rag_hits

    min_top = get_settings().rag_chat_min_relevance_score
    confident = filter_confident_rag_hits(hits, min_top_score=min_top, max_hits=len(hits))
    if not confident:
        return None
    sources = await build_rag_sources_payload(session, confident)
    if not sources:
        return None
    return {"type": "rag_sources", "kb_id": kb_id, "sources": sources}


def _rag_step_done(hits: list, *, kb_markdown: str = "", diag: dict | None = None) -> dict:
    diag = diag or {}
    scores = [h.score for h in hits] if hits else []
    avg = sum(scores) / len(scores) if scores else 0.0
    hit_count = len(hits)
    candidate_count = int(diag.get("candidate_count", 0))
    top_score = float(diag.get("top_score", 0.0))
    if hit_count == 0 and kb_markdown and "### 摘录" in kb_markdown:
        hit_count = len(re.findall(r"^### 摘录", kb_markdown, flags=re.MULTILINE))
        if hit_count > 0:
            scores = [0.0]
            avg = 0.0
    if hit_count > 0:
        detail = f"命中 {hit_count} 条片段，平均相关度 {avg:.0%}"
    elif candidate_count > 0:
        detail = f"检索到 {candidate_count} 条，相关度不足（最高 {top_score:.0%}），未注入"
    else:
        detail = "未命中相关内容"
    return _agent_step_sse(
        "rag_retrieval",
        status="done",
        detail=detail,
        meta={
            "hit_count": hit_count,
            "avg_score": round(avg, 3),
            "candidate_count": candidate_count,
            "top_score": round(top_score, 3),
        },
    )


async def _stream_prefetch_bundle(
    *,
    req: ChatRequest,
    user_id: str | None,
    kb_context: str,
    rag_task: asyncio.Task[tuple[str, list, dict]] | None,
    web_task: asyncio.Task[str] | None,
    arxiv_task: asyncio.Task[str] | None,
    s2_task: asyncio.Task[str] | None,
    attachment_task: asyncio.Task[str] | None,
    rag_hits: list,
    rag_diag: dict,
    plan_steps: list[str] | None = None,
) -> AsyncIterator[str | PrefetchResult]:
    if req.deep_research:

        async def load_rag() -> tuple[str, list]:
            if rag_task is not None:
                kb_md, hits, diag = await rag_task
            elif user_id and req.knowledge_base_id:
                kb_md, hits, diag = await _load_kb_context_isolated(
                    user_id,
                    req.knowledge_base_id,
                    req.message,
                )
            else:
                return "", []
            rag_hits.clear()
            rag_hits.extend(hits)
            rag_diag.clear()
            rag_diag.update(diag)
            return kb_md, hits

        async def fetch_web() -> str:
            if web_task is not None:
                return await web_task
            return await _fetch_web_markdown(req, user_id)

        from app.services.deep_research_executor import execute_deep_research_prefetch

        async for item in execute_deep_research_prefetch(
            req=req,
            user_id=user_id,
            kb_context=kb_context,
            load_rag=load_rag,
            rag_hits=rag_hits,
            rag_diag=rag_diag,
            agent_step_sse=_agent_step_sse,
            sse_event=_sse_event,
            rag_step_done=_rag_step_done,
            want_web_search=_want_web_search,
            fetch_web=fetch_web,
            plan_steps=plan_steps or [],
        ):
            yield item
        return

    attachment_md = ""

    async def _await_prefetch() -> PrefetchResult:
        nonlocal rag_hits
        nonlocal attachment_md

        errors: dict[str, str] = {}

        async def _get_rag() -> str:
            if rag_task is None:
                return (kb_context or "").strip()
            kb_from_rag, new_hits, diag = await rag_task
            rag_hits.clear()
            rag_hits.extend(new_hits)
            rag_diag.clear()
            rag_diag.update(diag)
            return kb_from_rag

        async def _empty_str() -> str:
            return ""

        async def _safe_text(step: str, awaitable: Awaitable[str]) -> str:
            try:
                return await awaitable
            except Exception as e:  # noqa: BLE001
                errors[step] = str(e) or e.__class__.__name__
                log_info("prefetch %s failed; continuing with remaining sources: %s", step, e)
                return ""

        async def _safe_rag() -> str:
            try:
                return await _get_rag()
            except Exception as e:  # noqa: BLE001
                errors["rag_retrieval"] = str(e) or e.__class__.__name__
                rag_diag["error"] = errors["rag_retrieval"]
                log_info("prefetch rag failed; continuing with remaining sources: %s", e)
                return (kb_context or "").strip()

        web_md, arxiv_md, s2_md, attachment_md, kb_from_rag = await asyncio.gather(
            _safe_text("web_search", web_task) if web_task is not None else _empty_str(),
            _safe_text("arxiv_search", arxiv_task) if arxiv_task is not None else _empty_str(),
            _safe_text("semantic_scholar", s2_task) if s2_task is not None else _empty_str(),
            _safe_text("attachment_parse", attachment_task)
            if attachment_task is not None
            else _empty_str(),
            _safe_rag(),
        )
        merged = merge_context_parts(kb_from_rag, web_md, arxiv_md, s2_md, attachment_md)
        return PrefetchResult(
            merged_context=merged,
            web_injected=bool((web_md or "").strip()),
            arxiv_injected=bool((arxiv_md or "").strip()),
            semantic_scholar_injected=bool((s2_md or "").strip()),
            errors=errors or None,
        )

    async for item in yield_prefetch_steps(
        req=req,
        user_id=user_id,
        rag_task=rag_task,
        web_task=web_task,
        arxiv_task=arxiv_task,
        s2_task=s2_task,
        await_prefetch=_await_prefetch,
        rag_hits=rag_hits,
        rag_diag=rag_diag,
        agent_step_sse=_agent_step_sse,
        sse_event=_sse_event,
        rag_step_done=_rag_step_done,
        want_web_search=_want_web_search,
    ):
        yield item


def _want_file_tools(req: ChatRequest, user_id: str | None = None) -> bool:
    """仅当用户消息含读写文件意图时才走 file_tools 编排（非流式）；普通问答直连 LLM 流式。"""
    if not settings.file_tools_enabled or not req.file_tools:
        return False
    if user_id:
        from app.services import mcp_registry

        if not mcp_registry.is_builtin_enabled(user_id, "file_writer"):
            return False
    msg = (req.message or "").strip()
    return has_read_intent(msg) or has_write_intent(msg)


async def _run_prefetch_with_retry(
    step: str,
    run: Callable[[], Awaitable[T]],
    *,
    retries: int = 1,
) -> T:
    """检索源级重试；全部失败后交给上层做单源降级，不中断其余并发源。"""
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return await run()
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt >= retries:
                break
            log_info(
                "prefetch %s failed, retrying (%s/%s): %s",
                step,
                attempt + 2,
                retries + 1,
                e,
            )
            await asyncio.sleep(0.3 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _want_web_search(req: ChatRequest, user_id: str | None = None) -> bool:
    if not settings.web_search_enabled or not req.web_search:
        return False
    if user_id:
        from app.services import mcp_registry

        if not mcp_registry.is_builtin_enabled(user_id, "web_search"):
            return False
    return True


def _want_external_mcp(req: ChatRequest, user_id: str | None = None) -> bool:
    if not settings.external_mcp_enabled or not req.external_mcp:
        return False
    if not user_id:
        return False
    from app.services.mcp_url_client import list_enabled_url_bindings

    return len(list_enabled_url_bindings(user_id)) > 0


def _external_mcp_noop_hint() -> str:
    return (
        "未调用外部 MCP：请先在「工具与集成」导入公网 HTTPS MCP URL 并开启开关，"
        "或在对话页打开「外部 MCP」。"
    )


def _mcp_tool_trace_sse(entry) -> dict:
    return {
        "type": "tool_result",
        "tool": entry.qualified_name,
        "ok": entry.ok,
        "result": entry.result,
        "meta": {"server": entry.server_name, "mcp_tool": entry.tool_name},
    }


@dataclass
class _ExternalMcpTurnResult:
    reasoning: str
    content: str
    traces: list
    disc_errs: list[str]
    tool_count: int
    aborted: bool = False


async def _iter_external_mcp_sse_events(
    messages: list,
    *,
    user_id: str,
) -> AsyncIterator[str | _ExternalMcpTurnResult]:
    """连接/调用外部 MCP；执行期间经 queue 实时推送 tool_result 等 SSE。"""
    from app.services.chat_external_mcp import (
        McpToolFatalError,
        McpToolTraceEntry,
        complete_chat_with_external_mcp,
        discover_external_mcp_tools,
    )

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    turn_holder: list[_ExternalMcpTurnResult | None] = [None]

    async def worker() -> None:
        traces: list[McpToolTraceEntry] = []

        async def on_tool(entry: McpToolTraceEntry) -> None:
            traces.append(entry)
            await queue.put(_mcp_tool_trace_sse(entry))

        async def on_status(detail: str) -> None:
            await queue.put(_agent_step_sse("external_mcp", status="running", detail=detail))

        try:
            discovery = await discover_external_mcp_tools(user_id)
            tool_count = len(discovery.tools)
            if discovery.discovery_errors and not discovery.tools:
                err_msg = "外部 MCP 连接失败：" + "；".join(discovery.discovery_errors)
                turn_holder[0] = _ExternalMcpTurnResult(
                    reasoning="",
                    content=f"**{err_msg}**",
                    traces=[],
                    disc_errs=discovery.discovery_errors,
                    tool_count=0,
                    aborted=True,
                )
                await queue.put({"type": "error", "message": err_msg})
                return

            if discovery.discovery_errors:
                await queue.put(
                    _agent_step_sse(
                        "external_mcp",
                        status="running",
                        detail="部分 MCP 可用：" + "；".join(discovery.discovery_errors),
                    ),
                )

            await queue.put(
                _agent_step_sse(
                    "external_mcp",
                    status="running",
                    detail=f"已发现 {tool_count} 个远程工具，调用中…"
                    if tool_count
                    else "未发现可用工具",
                ),
            )

            result = await complete_chat_with_external_mcp(
                messages,
                user_id=user_id,
                tool_bindings=discovery.tools,
                on_tool=on_tool,
                on_status=on_status,
            )
            turn_holder[0] = _ExternalMcpTurnResult(
                reasoning=result.reasoning,
                content=result.content,
                traces=traces,
                disc_errs=discovery.discovery_errors,
                tool_count=tool_count,
            )
        except McpToolFatalError as exc:
            err_msg = str(exc)
            if exc.entry not in traces:
                await queue.put(_mcp_tool_trace_sse(exc.entry))
            turn_holder[0] = _ExternalMcpTurnResult(
                reasoning="",
                content=f"**MCP 工具调用失败** {err_msg}",
                traces=traces,
                disc_errs=[err_msg],
                tool_count=len(traces),
                aborted=True,
            )
            await queue.put({"type": "error", "message": err_msg})
        except Exception as exc:  # noqa: BLE001
            err_msg = f"外部 MCP 异常：{exc!s}"
            turn_holder[0] = _ExternalMcpTurnResult(
                reasoning="",
                content=f"**{err_msg}**",
                traces=traces,
                disc_errs=[err_msg],
                tool_count=0,
                aborted=True,
            )
            await queue.put({"type": "error", "message": err_msg})
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(worker())
    while True:
        item = await queue.get()
        if item is sentinel:
            break
        yield _sse_event(item)
    await task

    if turn_holder[0] is None:
        turn_holder[0] = _ExternalMcpTurnResult(
            reasoning="",
            content="（外部 MCP 异常终止）",
            traces=[],
            disc_errs=[],
            tool_count=0,
            aborted=True,
        )
    yield turn_holder[0]


async def _consume_external_mcp_turn(
    messages: list,
    *,
    user_id: str,
    turn_out: list[_ExternalMcpTurnResult] | None = None,
) -> AsyncIterator[str]:
    """外部 MCP 分支：流式推送工具结果，失败时快速结束并下发 error。"""
    turn: _ExternalMcpTurnResult | None = None
    async for item in _iter_external_mcp_sse_events(messages, user_id=user_id):
        if isinstance(item, _ExternalMcpTurnResult):
            turn = item
            if turn_out is not None:
                turn_out.append(turn)
        else:
            yield item

    if turn is None:
        return

    detail = f"已注册 {turn.tool_count} 个远程工具，执行 {len(turn.traces)} 次调用"
    if turn.disc_errs:
        detail += f"；{len(turn.disc_errs)} 个服务异常"
    step_status = "error" if turn.aborted else "done"
    yield _sse_event(_agent_step_sse("external_mcp", status=step_status, detail=detail))

    if turn.aborted:
        if turn.disc_errs:
            yield _sse_event(_agent_step_sse("error", status="error", detail=turn.disc_errs[0]))
        body = turn.content or "（外部 MCP 失败）"
        for chunk in iter_text_chunks(body):
            yield _sse_event({"type": "delta", "text": chunk})
        return

    if not turn.traces:
        log_info("[external_mcp] %s", _external_mcp_noop_hint())
    yield _sse_event(_agent_step_sse("llm_generate", status="running", detail="模型生成中…"))
    if turn.reasoning.strip():
        for chunk in iter_text_chunks(turn.reasoning):
            yield _sse_event({"type": "thinking_delta", "text": chunk})
    body = turn.content or "（模型返回空正文）"
    for chunk in iter_text_chunks(body):
        yield _sse_event({"type": "delta", "text": chunk})


async def _merge_web_search_context(
    req: ChatRequest,
    kb_context: str,
    user_id: str | None = None,
) -> tuple[str, bool]:
    """返回 (合并后的上下文, 是否已注入联网块（含「无结果」说明）)。"""
    if not _want_web_search(req, user_id):
        if settings.web_search_enabled and req.web_search and user_id:
            from app.services import mcp_registry

            if not mcp_registry.is_builtin_enabled(user_id, "web_search"):
                log_info("[web_search] 跳过：请在「工具与集成」中开启 Web Search")
        elif settings.web_search_enabled and not req.web_search:
            log_info("[web_search] 跳过：对话未开启「联网搜索」开关")
        return kb_context, False
    from app.services.web_search_service import fetch_web_context_markdown

    web_md = await fetch_web_context_markdown(req.message)
    return _merge_kb_and_web(kb_context, web_md)


def _merge_kb_and_web(kb_context: str, web_md: str) -> tuple[str, bool]:
    if not (web_md or "").strip():
        return kb_context, False
    parts = [p for p in (kb_context, web_md) if p and p.strip()]
    return "\n\n".join(parts), True


async def _fetch_web_markdown(req: ChatRequest, user_id: str | None = None) -> str:
    if not _want_web_search(req, user_id):
        return ""
    from app.services.web_search_service import fetch_web_context_markdown

    return await fetch_web_context_markdown(req.message)


def _rag_diag_from_result(rag) -> dict:
    return {
        "candidate_count": int(getattr(rag, "candidate_count", 0) or 0),
        "top_score": float(getattr(rag, "top_candidate_score", 0.0) or 0.0),
    }


async def _load_kb_context(
    session: AsyncSession,
    user_id: str,
    kb_id: str | None,
    query: str,
) -> tuple[str, list, dict]:
    from app.services.rag_context import search_kb

    if not kb_id or not str(kb_id).strip():
        return "", [], {}
    rag = await search_kb(session, user_id, kb_id, query)
    return rag.markdown, rag.hits, _rag_diag_from_result(rag)


async def _load_kb_context_isolated(
    user_id: str,
    kb_id: str | None,
    query: str,
) -> tuple[str, list, dict]:
    """独立会话做 RAG，可与主 session 的会话写入并行，避免 SQLAlchemy 并发报错。"""
    from app.db.session import get_session_factory

    if not kb_id or not str(kb_id).strip():
        return "", [], {}
    factory = get_session_factory()
    async with factory() as rag_session:
        return await _load_kb_context(rag_session, user_id, kb_id, query)


async def _log_rag_retrieval_safe(
    session: AsyncSession,
    *,
    user_id: str,
    kb_id: str,
    query: str,
    conversation_id: str | None,
    hits: list,
) -> None:
    if not hits:
        return
    from app.services.rag_logging_service import log_rag_retrieval

    try:
        await log_rag_retrieval(
            session,
            user_id=user_id,
            kb_id=kb_id,
            query=query,
            conversation_id=conversation_id,
            hits=hits,
        )
        await session.commit()
    except Exception as e:  # noqa: BLE001
        log_info("[rag] 检索日志写入失败（不影响对话）: %s", e)


def _tool_trace_sse(entry: ToolTraceEntry) -> dict:
    return {
        "type": "tool_result",
        "tool": entry.name,
        "ok": entry.ok,
        "result": entry.result,
    }


def _file_log_sse(message: str) -> dict:
    return {"type": "file_log", "message": message}


def _emit_tool_events(entry: ToolTraceEntry) -> list[dict]:
    """仅推送工具结果；失败提示只写服务端日志，不在对话里显示绿框。"""
    if not entry.ok:
        log_info("[file_tools] %s", tool_log_message(entry))
        return []
    return [_tool_trace_sse(entry)]


def _has_write_trace(traces: list[ToolTraceEntry]) -> bool:
    return any(t.name in ("write_document", "write_markdown") and t.ok for t in traces)


def _file_tools_noop_hint(req: ChatRequest) -> str:
    msg = req.message.strip()
    if has_read_intent(msg):
        return "未读取到文件：请填写用户文件沙箱内的相对路径，例如 notes/项目聊天.md"
    if has_write_intent(msg):
        return "未执行文件写入：请在同一条消息里写清沙箱内路径（如 notes/a.md）和要保存的正文"
    return "未执行文件操作"


async def _stream_file_tools_turn(
    req: ChatRequest,
    messages: list,
    *,
    kb_context: str = "",
    user_id: str = "local",
) -> tuple[str, str, list[ToolTraceEntry]]:
    """读取/写入本地文件 + 必要时再调模型（含读文件后总结）。"""
    traces: list[ToolTraceEntry] = []
    user_texts = [req.message.strip(), *all_user_texts(messages)]
    seen_u: set[str] = set()
    unique_user_texts: list[str] = []
    for t in user_texts:
        if t and t not in seen_u:
            seen_u.add(t)
            unique_user_texts.append(t)

    user_blob = "\n\n".join(unique_user_texts)
    if has_read_intent(user_blob):
        read_traces, file_body, file_path = try_read_files_from_user_texts(
            unique_user_texts, user_id=user_id
        )
        traces.extend(read_traces)
        if file_body and file_path:
            log_info("[file_tools] 读文件后生成回复 path=%s", file_path)
            summary_messages = build_messages_for_file_task(
                req.message,
                file_path,
                file_body,
                kb_context=kb_context or None,
            )
            turn = await complete_chat_turn(summary_messages)
            return turn.reasoning, turn.content, traces

    direct = try_direct_write_from_user_message(req.message.strip(), user_id=user_id)
    if direct:
        traces.append(direct)

    async def on_tool(entry: ToolTraceEntry) -> None:
        traces.append(entry)

    result = await complete_chat_with_file_tools(messages, on_tool=on_tool, user_id=user_id)
    seen = {(t.name, t.arguments) for t in traces}
    for t in result.tool_traces:
        key = (t.name, t.arguments)
        if key not in seen:
            traces.append(t)
            seen.add(key)

    if not _has_write_trace(traces):
        post_calls = build_write_calls(unique_user_texts, result.content)
        if post_calls:
            log_info("[file_tools] 模型回复后备用写入 calls=%s", len(post_calls))
            traces.extend(execute_tool_calls(post_calls, user_id=user_id))

    return result.reasoning, result.content, traces


def _yield_traces(traces: list[ToolTraceEntry]) -> list[dict]:
    events: list[dict] = []
    for entry in traces:
        events.extend(_emit_tool_events(entry))
    return events


def _file_trace_payload(entry: ToolTraceEntry) -> dict:
    return {
        "tool": entry.name,
        "ok": entry.ok,
        "result": entry.result,
    }


def _mcp_trace_payload(entry) -> dict:
    return {
        "tool": entry.qualified_name,
        "ok": entry.ok,
        "result": entry.result,
        "server": entry.server_name,
        "mcp_tool": entry.tool_name,
    }


def _enqueue_chat_memory(
    *,
    conversation_id: str,
    user_id: str,
    user_text: str,
    assistant_text: str,
    assistant_message_id: str,
) -> None:
    try:
        from app.workers.memory_tasks import process_chat_memory_after_turn_task

        process_chat_memory_after_turn_task.delay(
            conversation_id=conversation_id,
            user_id=user_id,
            user_text=user_text,
            assistant_text=assistant_text,
            assistant_message_id=assistant_message_id,
        )
    except Exception:
        from app.services.chat_memory_worker import process_chat_memory_after_turn

        process_chat_memory_after_turn(
            conversation_id=conversation_id,
            user_id=user_id,
            user_text=user_text,
            assistant_text=assistant_text,
            assistant_message_id=assistant_message_id,
        )


async def iter_chat_stream(
    req: ChatRequest,
    *,
    kb_context: str = "",
    session: AsyncSession | None = None,
    user_id: str | None = None,
    expert_prompt: str | None = None,
    expert_id: str | None = None,
) -> AsyncIterator[str]:
    """
    SSE：`trace_id` → `conversation_id`（多轮）→ 可选 `thinking_delta` / `delta` → `done`。
    若传入 session + user_id，则启用 MySQL 会话、Redis 热数据与对话向量检索组装多轮 prompt。
    """
    trace_id = str(uuid.uuid4())
    yield _sse_event({"type": "trace_id", "trace_id": trace_id})

    if not settings.edgefn_api_key:
        yield _sse_event(
            {
                "type": "error",
                "message": "未配置 EDGEFN_API_KEY：请在 knowmind-server/.env 中设置第三方密钥后重启服务。",
            },
        )
        yield _sse_event({"type": "done"})
        return

    planning_context = ""
    if session is not None and user_id is not None and req.conversation_id:
        conv_for_plan = await session.get(Conversation, req.conversation_id)
        if conv_for_plan is not None and conv_for_plan.user_id == user_id:
            prior = await load_messages_ordered(session, conv_for_plan.id)
            recent = prior[-6:]
            planning_context = "\n\n".join(f"{m.role}: {m.content}" for m in recent)
            summaries_for_plan = await load_summaries_concat(session, conv_for_plan.id)
            facts_for_plan = await load_working_memory_concat(session, conv_for_plan.id)
            if summaries_for_plan:
                planning_context = (
                    f"历史摘要：\n{summaries_for_plan[-3000:]}\n\n最近消息：\n{planning_context}"
                )
            if facts_for_plan:
                planning_context = f"工作记忆：\n{facts_for_plan[-2000:]}\n\n{planning_context}"

    yield _sse_event(
        _agent_step_sse("planner", status="running", detail="LLM 正在根据会话上下文规划步骤…")
    )
    execution_plan = await build_llm_execution_plan(
        req,
        conversation_context=planning_context,
    )
    yield _sse_event(planner_sse(execution_plan))
    req = apply_plan_to_request(req, execution_plan)
    use_vector_memory = "memory_retrieval" in execution_plan.steps
    use_rag_retrieval = "rag_retrieval" in execution_plan.steps

    use_memory = session is not None and user_id is not None
    rag_hits: list = []
    rag_diag: dict = {}
    rag_sources_payload: list[dict] = []
    rag_task: asyncio.Task[tuple[str, list, dict]] | None = None
    web_task: asyncio.Task[str] | None = None
    arxiv_task: asyncio.Task[str] | None = None
    s2_task: asyncio.Task[str] | None = None
    attachment_task: asyncio.Task[str] | None = None
    if user_id and req.knowledge_base_id and use_rag_retrieval:
        rag_task = asyncio.create_task(
            _run_prefetch_with_retry(
                "rag_retrieval",
                lambda: _load_kb_context_isolated(
                    user_id,
                    req.knowledge_base_id or "",
                    req.message,
                ),
            ),
        )
    if user_id and req.attachment_ids and not req.deep_research:
        from app.services.chat_attachment_service import load_attachment_context_async

        attachment_task = asyncio.create_task(
            load_attachment_context_async(user_id, req.attachment_ids)
        )
    if _want_web_search(req, user_id) and not req.deep_research:
        web_task = asyncio.create_task(
            _run_prefetch_with_retry(
                "web_search",
                lambda: _fetch_web_markdown(req, user_id),
            ),
        )
    if want_arxiv(req, user_id) and not req.deep_research:
        arxiv_task = asyncio.create_task(
            _run_prefetch_with_retry(
                "arxiv_search",
                lambda: fetch_arxiv_md(req, user_id),
            ),
        )
    if want_semantic_scholar(req, user_id) and not req.deep_research:
        s2_task = asyncio.create_task(
            _run_prefetch_with_retry(
                "semantic_scholar",
                lambda: fetch_semantic_scholar_md(req, user_id),
            ),
        )

    async def _stream_prefetch():
        merged: PrefetchResult = PrefetchResult("", False, False, False)
        async for prefetch_item in _stream_prefetch_bundle(
            req=req,
            user_id=user_id,
            kb_context=kb_context,
            rag_task=rag_task,
            web_task=web_task,
            arxiv_task=arxiv_task,
            s2_task=s2_task,
            attachment_task=attachment_task,
            rag_hits=rag_hits,
            rag_diag=rag_diag,
            plan_steps=execution_plan.steps,
        ):
            if isinstance(prefetch_item, PrefetchResult):
                merged = prefetch_item
            else:
                yield prefetch_item
        yield merged

    if not use_memory:
        merged_kb, web_injected = "", False
        async for prefetch_out in _stream_prefetch():
            if isinstance(prefetch_out, PrefetchResult):
                merged_kb = prefetch_out.merged_context
                web_injected = prefetch_out.web_injected
            else:
                yield prefetch_out
        rag_sources_ev = await _sse_rag_sources(session, req.knowledge_base_id, rag_hits)
        if rag_sources_ev is not None:
            yield _sse_event(rag_sources_ev)
        web_hint = _want_web_search(req, user_id) and not web_injected
        messages = build_chat_messages(
            req.message,
            deep_research=req.deep_research,
            web_search=web_hint,
            kb_context=merged_kb or None,
            file_tools=_want_file_tools(req, user_id),
            external_mcp=bool(req.external_mcp),
            expert_prompt=expert_prompt,
        )
        try:
            if _want_file_tools(req, user_id):
                yield _sse_event(
                    _agent_step_sse("file_tools", status="running", detail="文件读写编排中…")
                )
                log_info("[file_tools] chat/stream 已启用 file_tools")
                reasoning, content, traces = await _stream_file_tools_turn(
                    req,
                    messages,
                    kb_context=merged_kb or "",
                    user_id=user_id or "anonymous",
                )
                for ev in _yield_traces(traces):
                    yield _sse_event(ev)
                yield _sse_event(
                    _agent_step_sse(
                        "file_tools",
                        status="done",
                        detail=f"完成 {len(traces)} 次工具调用" if traces else "未触发文件操作",
                    ),
                )
                if not traces:
                    log_info("[file_tools] %s", _file_tools_noop_hint(req))
                yield _sse_event(
                    _agent_step_sse("llm_generate", status="running", detail="模型生成中…")
                )
                if reasoning.strip():
                    for chunk in iter_text_chunks(reasoning):
                        yield _sse_event({"type": "thinking_delta", "text": chunk})
                for chunk in iter_text_chunks(content or "（模型返回空正文）"):
                    yield _sse_event({"type": "delta", "text": chunk})
            elif req.external_mcp and user_id and settings.external_mcp_enabled:
                if not _want_external_mcp(req, user_id):
                    yield _sse_event(
                        _agent_step_sse(
                            "external_mcp",
                            status="skipped",
                            detail="未启用带 URL 的外部 MCP，请先在工具页导入并打开开关",
                        ),
                    )
                    yield _sse_event(
                        _agent_step_sse("llm_generate", status="running", detail="模型流式生成中…")
                    )
                    async for reasoning_piece, content_piece in iter_edgefn_token_stream(messages):
                        if reasoning_piece:
                            yield _sse_event({"type": "thinking_delta", "text": reasoning_piece})
                        if content_piece:
                            yield _sse_event({"type": "delta", "text": content_piece})
                elif _want_external_mcp(req, user_id):
                    yield _sse_event(
                        _agent_step_sse(
                            "external_mcp", status="running", detail="连接远程 MCP 并发现工具…"
                        ),
                    )
                    log_info("[external_mcp] chat/stream 已启用")
                    async for sse_line in _consume_external_mcp_turn(messages, user_id=user_id):
                        yield sse_line
            else:
                yield _sse_event(
                    _agent_step_sse("llm_generate", status="running", detail="模型流式生成中…")
                )
                if web_injected:
                    log_info("[web_search] chat/stream 已注入联网结果")
                async for reasoning_piece, content_piece in iter_edgefn_token_stream(messages):
                    if reasoning_piece:
                        yield _sse_event({"type": "thinking_delta", "text": reasoning_piece})
                    if content_piece:
                        yield _sse_event({"type": "delta", "text": content_piece})
            yield _sse_event(_agent_step_sse("llm_generate", status="done", detail="正文输出完成"))
        except RuntimeError as e:
            yield _sse_event({"type": "error", "message": str(e)})
            yield _sse_event(_agent_step_sse("error", status="error", detail=str(e)))
        except Exception as e:  # noqa: BLE001
            msg = f"对话上游异常：{e!s}"
            yield _sse_event({"type": "error", "message": msg})
            yield _sse_event(_agent_step_sse("error", status="error", detail=msg))
        if session is not None and user_id is not None and req.knowledge_base_id:
            from app.services.usage_analytics_service import log_usage_safe, record_chat_turn

            await log_usage_safe(
                session,
                lambda: record_chat_turn(
                    session,
                    user_id=user_id,
                    kb_id=req.knowledge_base_id,
                    conversation_id=req.conversation_id,
                ),
            )
            await session.commit()
        if session is not None and user_id is not None and req.knowledge_base_id and rag_hits:
            await _log_rag_retrieval_safe(
                session,
                user_id=user_id,
                kb_id=req.knowledge_base_id,
                query=req.message,
                conversation_id=req.conversation_id,
                hits=rag_hits,
            )
        yield _sse_event({"type": "done"})
        return

    conv, is_new = await resolve_conversation(
        session,
        user_id=user_id,
        conversation_id=req.conversation_id,
        knowledge_base_id=req.knowledge_base_id,
        deep_research=req.deep_research,
        web_search=req.web_search,
        expert_id=expert_id,
    )
    yield _sse_event({"type": "conversation_id", "conversation_id": conv.id, "is_new": is_new})
    yield _sse_event(
        _agent_step_sse(
            "conversation",
            status="done",
            detail="新建会话" if is_new else "续聊会话",
            meta={"conversation_id": conv.id, "is_new": is_new},
        ),
    )

    attachment_rows: list[dict] = []
    if req.attachment_ids:
        from app.services.chat_attachment_service import attachment_metadata

        attachment_rows = attachment_metadata(user_id, req.attachment_ids)
    user_row = await append_message(
        session,
        conversation_id=conv.id,
        role="user",
        content=req.message.strip(),
        trace_id=None,
        attachments=attachment_rows,
    )
    await session.flush()
    n_user = await session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.conversation_id == conv.id,
            ChatMessage.role == "user",
        ),
    )
    if n_user == 1:
        conv_row = await session.get(Conversation, conv.id)
        if conv_row is not None and not (conv_row.title and str(conv_row.title).strip()):
            raw = req.message.strip().replace("\n", " ")
            conv_row.title = raw[:80] + ("…" if len(raw) > 80 else "")
    await session.commit()
    await session.refresh(user_row)

    if req.knowledge_base_id:
        from app.services.usage_analytics_service import log_usage_safe, record_chat_turn

        await log_usage_safe(
            session,
            lambda: record_chat_turn(
                session,
                user_id=user_id,
                kb_id=req.knowledge_base_id,
                conversation_id=conv.id,
            ),
        )
        await session.commit()

    merged_kb, web_injected = "", False
    async for prefetch_out in _stream_prefetch():
        if isinstance(prefetch_out, PrefetchResult):
            merged_kb = prefetch_out.merged_context
            web_injected = prefetch_out.web_injected
        else:
            yield prefetch_out
    rag_sources_ev = await _sse_rag_sources(session, req.knowledge_base_id, rag_hits)
    if rag_sources_ev is not None:
        rag_sources_payload = list(rag_sources_ev.get("sources") or [])
        yield _sse_event(rag_sources_ev)
    web_hint = _want_web_search(req, user_id) and not web_injected

    all_msgs = await load_messages_ordered(session, conv.id)
    prior_assistant_tail: str | None = None
    for m in reversed(all_msgs[:-1]):
        if m.role == "assistant":
            prior_assistant_tail = m.content
            break

    qtext = retrieval_query_text(
        user_message=req.message, prior_assistant_tail=prior_assistant_tail
    )
    summaries = await load_summaries_concat(session, conv.id)
    working_memory = await load_working_memory_concat(session, conv.id)

    retrieval_md = ""
    memory_hit_count = 0
    if use_vector_memory:
        yield _sse_event(
            _agent_step_sse("memory_retrieval", status="running", detail="对话记忆向量召回…")
        )
        try:
            q_vectors = await asyncio.to_thread(embed_texts, [qtext[:8000]])
            qvec = q_vectors[0]
            hits = await asyncio.to_thread(
                get_chat_memory_index().query_for_conversation,
                conversation_id=conv.id,
                user_id=user_id,
                query_embedding=qvec,
                top_k=settings.memory_retrieval_top_k,
            )
            memory_hit_count = len(hits)
            retrieval_md = format_retrieval_hits_markdown(hits)
            yield _sse_event(
                _agent_step_sse(
                    "memory_retrieval",
                    status="done",
                    detail=f"召回 {memory_hit_count} 条历史片段",
                    meta={"hit_count": memory_hit_count},
                ),
            )
        except Exception as e:  # noqa: BLE001
            retrieval_md = ""
            yield _sse_event(
                _agent_step_sse("memory_retrieval", status="error", detail=f"记忆召回失败：{e!s}"),
            )
    else:
        yield _sse_event(
            _agent_step_sse(
                "memory_retrieval", status="skipped", detail="Planner 判定无需历史向量召回"
            )
        )

    trimmed = apply_recent_message_window(all_msgs)
    pairs = history_pairs_from_messages(trimmed)
    messages = build_chat_messages_multi(
        deep_research=req.deep_research,
        web_search=web_hint,
        file_tools=_want_file_tools(req, user_id),
        external_mcp=bool(req.external_mcp),
        kb_context=merged_kb or None,
        memory_summaries=summaries,
        working_memory=working_memory,
        memory_retrieval=retrieval_md,
        history_pairs=pairs,
        expert_prompt=expert_prompt,
    )

    assistant_body_parts: list[str] = []
    persisted_tool_traces: list[dict] = []
    stream_ok = False
    try:
        if _want_file_tools(req, user_id):
            yield _sse_event(
                _agent_step_sse("file_tools", status="running", detail="文件读写编排中…")
            )
            log_info("[file_tools] chat/stream(多轮) 已启用 file_tools")
            reasoning, content, traces = await _stream_file_tools_turn(
                req,
                messages,
                kb_context=merged_kb or "",
                user_id=user_id,
            )
            persisted_tool_traces.extend(_file_trace_payload(t) for t in traces)
            for ev in _yield_traces(traces):
                yield _sse_event(ev)
            yield _sse_event(
                _agent_step_sse(
                    "file_tools",
                    status="done",
                    detail=f"完成 {len(traces)} 次工具调用" if traces else "未触发文件操作",
                ),
            )
            if not traces:
                log_info("[file_tools] %s", _file_tools_noop_hint(req))
            yield _sse_event(
                _agent_step_sse("llm_generate", status="running", detail="模型生成中…")
            )
            if reasoning.strip():
                for chunk in iter_text_chunks(reasoning):
                    yield _sse_event({"type": "thinking_delta", "text": chunk})
            body = content or "（模型返回空正文）"
            assistant_body_parts.append(body)
            for chunk in iter_text_chunks(body):
                yield _sse_event({"type": "delta", "text": chunk})
            stream_ok = True
        elif req.external_mcp and user_id and settings.external_mcp_enabled:
            if not _want_external_mcp(req, user_id):
                yield _sse_event(
                    _agent_step_sse(
                        "external_mcp",
                        status="skipped",
                        detail="未启用带 URL 的外部 MCP，请先在工具页导入并打开开关",
                    ),
                )
                yield _sse_event(
                    _agent_step_sse("llm_generate", status="running", detail="模型流式生成中…")
                )
                async for reasoning_piece, content_piece in iter_edgefn_token_stream(messages):
                    if reasoning_piece:
                        yield _sse_event({"type": "thinking_delta", "text": reasoning_piece})
                    if content_piece:
                        assistant_body_parts.append(content_piece)
                        yield _sse_event({"type": "delta", "text": content_piece})
                stream_ok = True
            else:
                yield _sse_event(
                    _agent_step_sse(
                        "external_mcp", status="running", detail="连接远程 MCP 并发现工具…"
                    ),
                )
                log_info("[external_mcp] chat/stream(多轮) 已启用")
                mcp_turn_box: list[_ExternalMcpTurnResult] = []
                async for sse_line in _consume_external_mcp_turn(
                    messages,
                    user_id=user_id,
                    turn_out=mcp_turn_box,
                ):
                    yield sse_line
                if mcp_turn_box:
                    t = mcp_turn_box[0]
                    persisted_tool_traces.extend(
                        _mcp_trace_payload(trace) for trace in t.traces
                    )
                    body = t.content or ("（外部 MCP 失败）" if t.aborted else "（模型返回空正文）")
                    assistant_body_parts.append(body)
                stream_ok = True
        else:
            yield _sse_event(
                _agent_step_sse("llm_generate", status="running", detail="模型流式生成中…")
            )
            if web_injected:
                log_info("[web_search] chat/stream(多轮) 已注入联网结果")
            async for reasoning_piece, content_piece in iter_edgefn_token_stream(messages):
                if reasoning_piece:
                    yield _sse_event({"type": "thinking_delta", "text": reasoning_piece})
                if content_piece:
                    assistant_body_parts.append(content_piece)
                    yield _sse_event({"type": "delta", "text": content_piece})
            stream_ok = True
        if stream_ok:
            yield _sse_event(_agent_step_sse("llm_generate", status="done", detail="正文输出完成"))
    except RuntimeError as e:
        yield _sse_event({"type": "error", "message": str(e)})
        yield _sse_event(_agent_step_sse("error", status="error", detail=str(e)))
    except Exception as e:  # noqa: BLE001
        msg = f"对话上游异常：{e!s}"
        yield _sse_event({"type": "error", "message": msg})
        yield _sse_event(_agent_step_sse("error", status="error", detail=msg))

    if stream_ok:
        assistant_text = "".join(assistant_body_parts).strip() or "（模型返回空正文）"
        asst_row = await append_message(
            session,
            conversation_id=conv.id,
            role="assistant",
            content=assistant_text,
            trace_id=trace_id,
            reply_to_message_id=user_row.id,
            citations=rag_sources_payload,
            tool_traces=persisted_tool_traces,
        )
        conv_ref = await session.get(Conversation, conv.id)
        if conv_ref is not None:
            ut = int(user_row.token_est or approx_token_count(user_row.content))
            at = approx_token_count(assistant_text)
            conv_ref.acc_turns_since_summary = int(conv_ref.acc_turns_since_summary or 0) + 1
            conv_ref.acc_tokens_since_summary = (
                int(conv_ref.acc_tokens_since_summary or 0) + ut + at
            )
            conv_ref.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(asst_row)

        full = await load_messages_ordered(session, conv.id)
        await set_recent_messages(conv.id, rows_for_redis(full))

        _enqueue_chat_memory(
            conversation_id=conv.id,
            user_id=user_id,
            user_text=user_row.content,
            assistant_text=assistant_text,
            assistant_message_id=asst_row.id,
        )
        yield _sse_event({"type": "message_saved", "message_id": asst_row.id})

    if req.knowledge_base_id and rag_hits:
        await _log_rag_retrieval_safe(
            session,
            user_id=user_id,
            kb_id=req.knowledge_base_id,
            query=req.message,
            conversation_id=conv.id,
            hits=rag_hits,
        )

    yield _sse_event({"type": "done"})


async def run_chat(req: ChatRequest, *, kb_context: str = "") -> ChatResponse:
    """同步 JSON：一次性返回模型正文（含可选推理过程，前置在 reply 中）。"""
    trace_id = str(uuid.uuid4())

    if not settings.edgefn_api_key:
        return ChatResponse(
            reply="（配置缺失）请在服务器环境变量 EDGEFN_API_KEY 中填写 EdgeFN 密钥并重启 API。",
            trace_id=trace_id,
        )

    merged_kb, web_injected = await _merge_web_search_context(req, kb_context, None)
    web_hint = _want_web_search(req, None) and not web_injected
    messages = build_chat_messages(
        req.message,
        deep_research=req.deep_research,
        web_search=web_hint,
        kb_context=merged_kb or None,
        file_tools=_want_file_tools(req),
    )
    try:
        if _want_file_tools(req):
            reasoning, content, _traces = await _stream_file_tools_turn(
                req,
                messages,
                kb_context=merged_kb or "",
                user_id="anonymous",
            )
        else:
            reasoning, content, _raw = await complete_chat(messages)
    except RuntimeError as e:
        return ChatResponse(reply=f"（调用失败）{e}", trace_id=trace_id)
    except Exception as e:  # noqa: BLE001
        return ChatResponse(reply=f"（调用失败）{e!s}", trace_id=trace_id)

    parts: list[str] = []
    if reasoning.strip():
        parts.append(reasoning.strip())
        parts.append("\n\n---\n\n")
    parts.append(content if content.strip() else "（模型返回空正文）")
    return ChatResponse(reply="".join(parts), trace_id=trace_id)
