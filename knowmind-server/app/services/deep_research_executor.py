"""深度研究：RAG 优先，其余检索步骤并行执行。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from app.models.schemas import ChatRequest
from app.services.chat_prefetch import (
    PrefetchResult,
    fetch_arxiv_md,
    fetch_semantic_scholar_md,
    merge_context_parts,
    sources_from_markdown,
    want_arxiv,
    want_semantic_scholar,
)

MAX_RETRIES = 1


async def _retry_fetch(run: Callable[[], Awaitable[str]]) -> str:
    last_err = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await run()
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.3)
    if last_err:
        raise RuntimeError(last_err)
    return ""


async def execute_deep_research_prefetch(
    *,
    req: ChatRequest,
    user_id: str | None,
    kb_context: str,
    load_rag: Callable[[], Awaitable[tuple[str, list]]],
    rag_hits: list,
    rag_diag: dict,
    agent_step_sse: Callable[..., dict],
    sse_event: Callable[[dict], str],
    rag_step_done: Callable[..., dict],
    want_web_search: Callable[[ChatRequest, str | None], bool],
    fetch_web: Callable[[], Awaitable[str]],
    plan_steps: list[str],
) -> AsyncIterator[str | PrefetchResult]:
    selected_steps = set(plan_steps)
    lines: list[str] = []

    def emit(payload: dict) -> None:
        lines.append(sse_event(payload))

    kb_md = (kb_context or "").strip()
    web_md = ""
    arxiv_md = ""
    s2_md = ""

    if "rag_retrieval" in selected_steps and req.knowledge_base_id:
        emit(agent_step_sse("rag_retrieval", status="running", detail="Hybrid RAG 检索中…"))
        try:
            kb_md, hits = await load_rag()
            rag_hits.clear()
            rag_hits.extend(hits)
        except Exception as e:  # noqa: BLE001
            emit(agent_step_sse("rag_retrieval", status="error", detail=f"失败：{e!s}"))
        else:
            emit(rag_step_done(rag_hits, kb_markdown=kb_md, diag=rag_diag))

    parallel_jobs: list[tuple[str, Callable[[], Awaitable[str]]]] = []
    if "arxiv_search" in selected_steps and want_arxiv(req, user_id):
        parallel_jobs.append(("arxiv_search", lambda: fetch_arxiv_md(req, user_id)))
    if "semantic_scholar" in selected_steps and want_semantic_scholar(req, user_id):
        parallel_jobs.append(("semantic_scholar", lambda: fetch_semantic_scholar_md(req, user_id)))
    if "web_search" in selected_steps and want_web_search(req, user_id):
        parallel_jobs.append(("web_search", fetch_web))

    attachment_task: asyncio.Task[str] | None = None
    if user_id and req.attachment_ids:
        from app.services.chat_attachment_service import load_attachment_context_async

        emit(agent_step_sse("attachment_parse", status="running", detail="解析附件 / 识图中…"))
        attachment_task = asyncio.create_task(
            load_attachment_context_async(user_id, req.attachment_ids)
        )

    if parallel_jobs:
        for step, _ in parallel_jobs:
            emit(agent_step_sse(step, status="running", detail="并行检索中…"))

        async def _run_named(
            step: str, fetch: Callable[[], Awaitable[str]]
        ) -> tuple[str, str, str | None]:
            try:
                return step, await _retry_fetch(fetch), None
            except Exception as e:  # noqa: BLE001
                return step, "", str(e)

        results = await asyncio.gather(*(_run_named(step, fetch) for step, fetch in parallel_jobs))
        done_labels = {
            "arxiv_search": ("已注入 arXiv 结果", "无 arXiv 结果"),
            "semantic_scholar": ("已注入 Semantic Scholar 结果", "无 S2 结果"),
            "web_search": ("已注入联网结果", "无联网结果"),
        }
        for step, md, err in results:
            if err:
                emit(agent_step_sse(step, status="error", detail=f"失败：{err}"))
                continue
            ok_label, empty_label = done_labels.get(step, ("完成", "无结果"))
            emit(
                agent_step_sse(
                    step,
                    status="done",
                    detail=ok_label if md.strip() else empty_label,
                    meta={"injected": bool(md.strip())},
                ),
            )
            if step == "arxiv_search":
                arxiv_md = md
            elif step == "semantic_scholar":
                s2_md = md
            elif step == "web_search":
                web_md = md

    attachment_md = ""
    if attachment_task is not None:
        try:
            attachment_md = await attachment_task
            has_att = bool(attachment_md.strip())
            emit(
                agent_step_sse(
                    "attachment_parse",
                    status="done",
                    detail="附件已解析并注入上下文" if has_att else "附件解析无有效内容",
                    meta={"injected": has_att},
                ),
            )
        except Exception as e:  # noqa: BLE001
            emit(agent_step_sse("attachment_parse", status="error", detail=f"失败：{e!s}"))

    for line in lines:
        yield line

    merged = merge_context_parts(kb_md, web_md, arxiv_md, s2_md, attachment_md)
    yield PrefetchResult(
        merged_context=merged,
        web_injected=bool(web_md.strip()),
        arxiv_injected=bool(arxiv_md.strip()),
        semantic_scholar_injected=bool(s2_md.strip()),
        sources=[
            *sources_from_markdown(web_md, "web"),
            *sources_from_markdown(arxiv_md, "arxiv"),
            *sources_from_markdown(s2_md, "semantic_scholar"),
        ],
    )
