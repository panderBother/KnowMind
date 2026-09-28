"""对话上下文预取：RAG、联网、学术检索并行合并。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from app.models.schemas import ChatRequest


@dataclass
class PrefetchResult:
    merged_context: str
    web_injected: bool
    arxiv_injected: bool
    semantic_scholar_injected: bool
    errors: dict[str, str] | None = None
    sources: list[dict] = field(default_factory=list)


def sources_from_markdown(markdown: str, source_type: str) -> list[dict]:
    """把联网/学术检索 Markdown 归一化为统一来源面板数据。"""
    rows: list[dict] = []
    current: dict | None = None
    paragraph_lines: list[str] = []

    def finish() -> None:
        nonlocal current, paragraph_lines
        if current and current.get("url"):
            if paragraph_lines:
                current["snippet"] = " ".join(paragraph_lines)[:500]
            rows.append(current)
        current = None
        paragraph_lines = []

    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^###\s+\[(\d+)]\s+(.+)$", line)
        if heading:
            finish()
            current = {
                "index": len(rows) + 1,
                "title": heading.group(2).strip(),
                "snippet": "",
                "source_type": source_type,
            }
            continue
        if current is None or not line:
            continue
        url_match = re.match(
            r"^-\s+(?:\*\*)?链接(?:\*\*)?[：:]\s*(https?://\S+)", line
        )
        if url_match:
            current["url"] = url_match.group(1).rstrip(".,，。)")
            continue
        summary_match = re.match(r"^-\s+(?:\*\*)?摘要(?:\*\*)?[：:]\s*(.+)$", line)
        if summary_match:
            paragraph_lines.append(summary_match.group(1).strip())
        elif not line.startswith("-"):
            paragraph_lines.append(line)
    finish()
    return rows[:10]


def merge_context_parts(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p and p.strip())


def want_arxiv(req: ChatRequest, user_id: str | None) -> bool:
    from app.core.config import settings

    if not settings.arxiv_search_enabled or not req.arxiv:
        return False
    if user_id:
        from app.services import mcp_registry

        if not mcp_registry.is_builtin_enabled(user_id, "arxiv"):
            return False
    return True


def want_semantic_scholar(req: ChatRequest, user_id: str | None) -> bool:
    from app.core.config import settings

    if not settings.semantic_scholar_enabled or not req.semantic_scholar:
        return False
    if user_id:
        from app.services import mcp_registry

        if not mcp_registry.is_builtin_enabled(user_id, "semantic_scholar"):
            return False
    return True


async def fetch_arxiv_md(req: ChatRequest, user_id: str | None) -> str:
    if not want_arxiv(req, user_id):
        return ""
    from app.services.academic_search_service import fetch_arxiv_context_markdown

    return await fetch_arxiv_context_markdown(req.message)


async def fetch_semantic_scholar_md(req: ChatRequest, user_id: str | None) -> str:
    if not want_semantic_scholar(req, user_id):
        return ""
    from app.services.academic_search_service import fetch_semantic_scholar_context_markdown

    return await fetch_semantic_scholar_context_markdown(req.message)


async def yield_prefetch_steps(
    *,
    req: ChatRequest,
    user_id: str | None,
    rag_task: asyncio.Task[tuple[str, list, dict]] | None,
    web_task: asyncio.Task[str] | None,
    arxiv_task: asyncio.Task[str] | None,
    s2_task: asyncio.Task[str] | None,
    await_prefetch: Callable[[], Awaitable[PrefetchResult]],
    rag_hits: list,
    rag_diag: dict,
    agent_step_sse: Callable[..., dict],
    sse_event: Callable[[dict], str],
    rag_step_done: Callable[..., dict],
    want_web_search: Callable[[ChatRequest, str | None], bool],
) -> AsyncIterator[str | PrefetchResult]:
    if rag_task is not None:
        yield sse_event(
            agent_step_sse("rag_retrieval", status="running", detail="Hybrid RAG 检索中…")
        )
    if web_task is not None:
        yield sse_event(agent_step_sse("web_search", status="running", detail="联网搜索中…"))
    if arxiv_task is not None:
        yield sse_event(agent_step_sse("arxiv_search", status="running", detail="arXiv 检索中…"))
    if s2_task is not None:
        yield sse_event(
            agent_step_sse("semantic_scholar", status="running", detail="Semantic Scholar 检索中…")
        )
    if req.attachment_ids and user_id:
        yield sse_event(
            agent_step_sse("attachment_parse", status="running", detail="解析附件 / 识图中…")
        )

    result = await await_prefetch()

    if req.attachment_ids and user_id:
        has_att = "## 用户上传附件" in (result.merged_context or "")
        yield sse_event(
            agent_step_sse(
                "attachment_parse",
                status="error" if result.errors and "attachment_parse" in result.errors else "done",
                detail=(
                    f"降级：{result.errors['attachment_parse']}"
                    if result.errors and "attachment_parse" in result.errors
                    else "附件已解析并注入上下文"
                    if has_att
                    else "附件解析无有效内容"
                ),
                meta={
                    "injected": has_att,
                    "degraded": bool(result.errors and "attachment_parse" in result.errors),
                },
            ),
        )

    if rag_task is not None:
        if result.errors and "rag_retrieval" in result.errors:
            yield sse_event(
                agent_step_sse(
                    "rag_retrieval",
                    status="error",
                    detail=f"降级：{result.errors['rag_retrieval']}",
                    meta={"hit_count": len(rag_hits), "degraded": True},
                ),
            )
        else:
            yield sse_event(
                rag_step_done(rag_hits, kb_markdown=result.merged_context, diag=rag_diag)
            )
    elif req.knowledge_base_id:
        yield sse_event(
            agent_step_sse("rag_retrieval", status="skipped", detail="未绑定知识库或未登录")
        )
    if web_task is not None:
        yield sse_event(
            agent_step_sse(
                "web_search",
                status="error" if result.errors and "web_search" in result.errors else "done",
                detail=(
                    f"降级：{result.errors['web_search']}"
                    if result.errors and "web_search" in result.errors
                    else "已注入联网结果"
                    if result.web_injected
                    else "无可用联网结果"
                ),
                meta={
                    "injected": result.web_injected,
                    "degraded": bool(result.errors and "web_search" in result.errors),
                },
            ),
        )
    elif want_web_search(req, user_id):
        yield sse_event(agent_step_sse("web_search", status="skipped", detail="联网搜索未返回结果"))
    if arxiv_task is not None:
        yield sse_event(
            agent_step_sse(
                "arxiv_search",
                status="error" if result.errors and "arxiv_search" in result.errors else "done",
                detail=(
                    f"降级：{result.errors['arxiv_search']}"
                    if result.errors and "arxiv_search" in result.errors
                    else "已注入 arXiv 结果"
                    if result.arxiv_injected
                    else "无 arXiv 结果"
                ),
                meta={
                    "injected": result.arxiv_injected,
                    "degraded": bool(result.errors and "arxiv_search" in result.errors),
                },
            ),
        )
    elif want_arxiv(req, user_id):
        yield sse_event(agent_step_sse("arxiv_search", status="skipped", detail="arXiv 未返回结果"))
    if s2_task is not None:
        yield sse_event(
            agent_step_sse(
                "semantic_scholar",
                status="error" if result.errors and "semantic_scholar" in result.errors else "done",
                detail=(
                    f"降级：{result.errors['semantic_scholar']}"
                    if result.errors and "semantic_scholar" in result.errors
                    else "已注入 Semantic Scholar 结果"
                    if result.semantic_scholar_injected
                    else "无 S2 结果"
                ),
                meta={
                    "injected": result.semantic_scholar_injected,
                    "degraded": bool(result.errors and "semantic_scholar" in result.errors),
                },
            ),
        )
    elif want_semantic_scholar(req, user_id):
        yield sse_event(
            agent_step_sse(
                "semantic_scholar", status="skipped", detail="Semantic Scholar 未返回结果"
            )
        )
    yield result
