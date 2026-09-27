"""知识库混合检索：Chroma 向量 + Whoosh BM25 + RRF 融合 + BGE-Reranker 精排。"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.indexing.vector_factory import get_vector_index
from app.indexing.whoosh_index import whoosh_search
from app.ingest.embedding import embed_texts
from app.ingest.rerank import rerank_candidates
from app.models.orm import Document, KnowledgeItem
from app.services.knowledge_base_service import get_knowledge_base
from app.services.rag_logging_service import distance_to_score

log = logging.getLogger(__name__)


class SearchError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass
class HybridSearchHit:
    item_id: str
    chunk_id: str
    doc_id: str
    title: str
    text: str
    snippet: str
    score: float
    source_type: str
    page: int | None
    tags: list[str]


def rrf_merge(
    *ranked_lists: list[dict[str, Any]],
    key: str = "chunk_id",
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion：多路检索结果去重融合。"""
    scores: dict[str, float] = {}
    merged: dict[str, dict[str, Any]] = {}

    for lst in ranked_lists:
        for rank, item in enumerate(lst, start=1):
            cid = str(item.get(key) or "")
            if not cid:
                continue
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in merged:
                merged[cid] = dict(item)
            else:
                prev = merged[cid]
                if len(str(item.get("text") or "")) > len(str(prev.get("text") or "")):
                    merged[cid] = {**prev, **item}

    ordered = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
    out: list[dict[str, Any]] = []
    for cid in ordered:
        row = dict(merged[cid])
        row["score"] = scores[cid]
        row["rrf_score"] = scores[cid]
        out.append(row)
    return out


def _snippet(text: str, max_len: int = 200) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= max_len:
        return t
    return t[:max_len] + "…"


def _tags_match(item_tags: list | None, required: list[str]) -> bool:
    if not required:
        return True
    have = {str(t).strip().lower() for t in (item_tags or []) if str(t).strip()}
    return all(r.strip().lower() in have for r in required)


async def _vector_hits(kb_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    try:
        q_vectors = await asyncio.to_thread(embed_texts, [query[:8000]])
        raw_vector = get_vector_index().query_similar(
            kb_id=kb_id,
            query_embedding=q_vectors[0],
            top_k=top_k,
        )
        for h in raw_vector:
            hits.append(
                {
                    "chunk_id": str(h.get("chunk_id") or ""),
                    "text": str(h.get("text") or ""),
                    "doc_id": str(h.get("doc_id") or ""),
                    "revision_id": str(h.get("revision_id") or ""),
                    "item_id": str(h.get("item_id") or ""),
                    "page": int(h.get("page") or 0),
                    "score": distance_to_score(h.get("distance")),
                    "vector_score": distance_to_score(h.get("distance")),
                },
            )
    except Exception as e:
        log.exception("hybrid_search vector failed: %s", e)
    return hits


def _bm25_hits(root: str, kb_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
    rows = whoosh_search(
        root,
        kb_id=kb_id,
        query=query,
        top_k=top_k,
        lifecycle_status="published",
    )
    for h in rows:
        h["bm25_score"] = h.get("score")
    return rows


def _entity_query(query: str) -> str:
    """保留实体查询字符，去掉常见问句尾巴。"""
    value = re.sub(r"[\s？?，。,.、!！：:；;‘’\"“”'（）()]+", "", query or "")
    return re.sub(r"(是谁|是什么人|介绍一下|简介|个人信息|的情况)$", "", value)


async def _fuzzy_title_hits(
    session: AsyncSession,
    kb_id: str,
    query: str,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """对短实体/人名提供一字错别字容错，避免 dense/BM25 同时失配。"""
    entity = _entity_query(query)
    if len(entity) < 2 or len(entity) > 32:
        return []
    rows = await session.execute(
        select(KnowledgeItem).where(
            KnowledgeItem.kb_id == kb_id,
            KnowledgeItem.lifecycle_status == "published",
        ),
    )
    scored: list[tuple[float, float, KnowledgeItem]] = []
    negative_markers = (
        "没有明确",
        "信息未详",
        "未详尽记录",
        "无法确认",
        "可能原因包括拼写错误",
        "请提供更多",
        "缺少上下文",
    )
    for item in rows.scalars().all():
        title = (item.title or "").strip()
        if not title:
            continue
        # Compare against the title and every same-length window in the query.
        score = difflib.SequenceMatcher(None, entity, title).ratio()
        if len(title) >= len(entity):
            score = max(
                score,
                max(
                    (
                        difflib.SequenceMatcher(None, entity, title[i : i + len(entity)]).ratio()
                        for i in range(len(title) - len(entity) + 1)
                    ),
                    default=0.0,
                ),
            )
        elif len(entity) >= len(title):
            score = max(
                score,
                max(
                    (
                        difflib.SequenceMatcher(None, entity[i : i + len(title)], title).ratio()
                        for i in range(len(entity) - len(title) + 1)
                    ),
                    default=0.0,
                ),
            )
        if score >= 0.60:
            content = (item.content or item.summary or "").strip()
            negative_count = sum(1 for marker in negative_markers if marker in content)
            richness = min(len(content), 2000) / 2000.0
            intent_bonus = 0.0
            if any(word in title for word in ("个人信息", "简历", "简介", "档案")):
                intent_bonus = 0.20
            elif any(word in title for word in ("技术栈", "经历", "项目")):
                intent_bonus = 0.08
            rank_score = score + richness * 0.18 + intent_bonus - negative_count * 0.35
            scored.append((rank_score, score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    hits: list[dict[str, Any]] = []
    for _rank_score, score, item in scored[:limit]:
        hits.append(
            {
                "chunk_id": f"entity:{item.id}",
                "item_id": item.id,
                "doc_id": str(item.document_id or ""),
                "text": item.content or item.summary or item.title,
                "page": int(item.page or 0),
                "score": score,
                "vector_score": score,
                "bm25_score": max(1.0, score),
                "entity_fuzzy": True,
            },
        )
    return hits


def _top_bm25_score(bm25_hits: list[dict[str, Any]]) -> float:
    if not bm25_hits:
        return 0.0
    return max(float(h.get("score") or 0) for h in bm25_hits)


_KEYWORD_CONFIDENT_BM25 = 0.35


def _fuse_candidates(
    vector_hits: list[dict[str, Any]],
    bm25_hits: list[dict[str, Any]],
    *,
    embed_mode: str = "bge",
    strict: bool = False,
) -> tuple[list[dict[str, Any]], set[str], dict[str, float]]:
    """RRF 融合；strict=True 时用于对话 RAG，禁止纯 BM25 与弱向量灌入候选池。"""
    settings = get_settings()
    bm25_ids = {str(h.get("chunk_id") or "") for h in bm25_hits}
    vector_score_by_id = {
        str(h.get("chunk_id") or ""): float(h.get("score") or 0) for h in vector_hits
    }
    min_vec = settings.rag_chat_min_relevance_score if strict else settings.rag_min_relevance_score
    top_vector = max(vector_hits, key=lambda h: float(h.get("score") or 0)) if vector_hits else None
    top_vec_score = float(top_vector.get("score") or 0) if top_vector else 0.0
    top_bm25 = _top_bm25_score(bm25_hits)
    keyword_confident = top_bm25 >= _KEYWORD_CONFIDENT_BM25

    if embed_mode != "hash" and strict:
        if not vector_hits and not keyword_confident:
            return [], bm25_ids, vector_score_by_id
        if vector_hits and top_vec_score < min_vec and not keyword_confident:
            return [], bm25_ids, vector_score_by_id

    if vector_hits and bm25_hits:
        fused = rrf_merge(vector_hits, bm25_hits)
    elif bm25_hits:
        if embed_mode == "hash" or not strict or keyword_confident:
            fused = rrf_merge(bm25_hits)
        else:
            fused = []
    elif vector_hits:
        if top_vec_score >= min_vec:
            fused = rrf_merge(vector_hits)
        else:
            fused = []
    else:
        fused = []

    return fused, bm25_ids, vector_score_by_id


def _semantic_relevance_score(
    h: dict[str, Any],
    vector_score_by_id: dict[str, float],
) -> float:
    """展示与门槛用的语义相关度：Rerank > 向量；不用 RRF 融合分（通常仅 0.01–0.05）。"""
    if h.get("rerank_score") is not None:
        return float(h["rerank_score"])
    cid = str(h.get("chunk_id") or "")
    if h.get("vector_score") is not None:
        return float(h["vector_score"])
    return vector_score_by_id.get(cid, 0.0)


def _passes_relevance_gate(
    chunk_id: str,
    *,
    bm25_ids: set[str],
    bm25_score_by_id: dict[str, float],
    vector_score_by_id: dict[str, float],
    rerank_applied: bool,
    h: dict[str, Any],
    embed_mode: str,
    allow_keyword_fallback: bool,
) -> bool:
    settings = get_settings()
    semantic = _semantic_relevance_score(h, vector_score_by_id)
    min_score = settings.rag_min_relevance_score
    if embed_mode == "hash":
        return chunk_id in bm25_ids or semantic >= min_score
    threshold = (
        settings.rerank_min_score
        if rerank_applied and settings.rerank_min_score is not None
        else min_score
    )
    if semantic >= threshold:
        return True
    if allow_keyword_fallback and chunk_id in bm25_ids:
        return bm25_score_by_id.get(chunk_id, 0.0) >= 0.35
    return False


async def hybrid_search(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    q: str,
    *,
    limit: int = 20,
    category_id: str | None = None,
    tags: list[str] | None = None,
    rerank: bool | None = None,
    strict_fusion: bool = False,
    allow_keyword_fallback: bool = True,
) -> list[HybridSearchHit]:
    """
    混合检索主流程：
    1. Chroma 语义 Top-K（BGE-M3）
    2. Whoosh BM25 关键词 Top-K
    3. RRF 融合
    4. BGE-Reranker 精排（可配置关闭）
    5. MySQL 补全元数据 + 分类/标签筛选
    """
    query = (q or "").strip()
    if not query:
        raise SearchError("检索词不能为空", 422)

    await get_knowledge_base(session, user_id, kb_id)

    settings = get_settings()
    cap = max(1, min(int(limit), 50))
    vector_k = max(1, min(int(settings.rag_vector_top_k), 64))
    bm25_k = max(1, min(int(settings.rag_bm25_top_k), 64))
    rerank_pool_k = min(40, vector_k + bm25_k)

    vector_hits, bm25_hits, fuzzy_title_hits = await asyncio.gather(
        _vector_hits(kb_id, query, vector_k),
        asyncio.to_thread(
            _bm25_hits,
            settings.whoosh_index_root,
            kb_id,
            query,
            bm25_k,
        ),
        _fuzzy_title_hits(session, kb_id, query),
    )

    if fuzzy_title_hits:
        fuzzy_ids = {str(hit.get("chunk_id") or "") for hit in fuzzy_title_hits}
        bm25_hits = fuzzy_title_hits + [
            hit for hit in bm25_hits if str(hit.get("chunk_id") or "") not in fuzzy_ids
        ]

    embed_mode = (settings.embedding_mode or "bge").strip().lower()
    fused, bm25_ids, vector_score_by_id = _fuse_candidates(
        vector_hits,
        bm25_hits,
        embed_mode=embed_mode,
        strict=strict_fusion,
    )
    if fuzzy_title_hits:
        fuzzy_ids = {str(hit.get("chunk_id") or "") for hit in fuzzy_title_hits}
        fused = fuzzy_title_hits + [
            hit for hit in fused if str(hit.get("chunk_id") or "") not in fuzzy_ids
        ]
    bm25_score_by_id = {str(h.get("chunk_id") or ""): float(h.get("score") or 0) for h in bm25_hits}
    do_rerank = rerank if rerank is not None else settings.rerank_enabled
    rerank_input = fused[:rerank_pool_k] if fused else []
    if do_rerank and embed_mode != "hash" and rerank_input:
        reranked = await asyncio.to_thread(rerank_candidates, query, rerank_input)
        if len(fused) > rerank_pool_k:
            reranked.extend(fused[rerank_pool_k:])
    else:
        reranked = fused
    if fuzzy_title_hits:
        fuzzy_ids = {str(hit.get("chunk_id") or "") for hit in fuzzy_title_hits}
        # Exact/near-exact title matches are authoritative entity hits; keep
        # them ahead of generic semantic candidates even if the reranker is
        # uncertain about a short misspelled name.
        reranked = fuzzy_title_hits + [
            hit for hit in reranked if str(hit.get("chunk_id") or "") not in fuzzy_ids
        ]
    rerank_applied = bool(
        do_rerank
        and embed_mode != "hash"
        and reranked
        and any(r.get("rerank_score") is not None for r in reranked),
    )
    trimmed = reranked[:cap]

    item_ids = [str(h.get("item_id") or "") for h in trimmed if h.get("item_id")]
    doc_ids = [str(h.get("doc_id") or "") for h in trimmed if h.get("doc_id")]
    items_by_id: dict[str, KnowledgeItem] = {}
    if item_ids:
        stmt = select(KnowledgeItem).where(
            KnowledgeItem.kb_id == kb_id,
            KnowledgeItem.id.in_(item_ids),
        )
        rows = await session.execute(stmt)
        for item in rows.scalars().all():
            items_by_id[item.id] = item

    docs_by_id: dict[str, Document] = {}
    if doc_ids:
        stmt = select(Document).where(
            Document.kb_id == kb_id,
            Document.id.in_(doc_ids),
        )
        rows = await session.execute(stmt)
        for doc in rows.scalars().all():
            docs_by_id[doc.id] = doc

    tag_filter = [t for t in (tags or []) if t.strip()]
    results: list[HybridSearchHit] = []

    for h in trimmed:
        item_id = str(h.get("item_id") or "").strip()
        item = items_by_id.get(item_id) if item_id else None
        doc_id = str(h.get("doc_id") or "").strip()
        hit_revision_id = str(h.get("revision_id") or "").strip()
        doc = docs_by_id.get(doc_id) if doc_id else None

        if item_id:
            if item is None or item.lifecycle_status != "published":
                continue
        elif doc_id:
            if doc is None:
                continue
        else:
            continue

        if doc_id:
            if doc is None or doc.lifecycle_status != "active":
                continue
            if doc.current_revision_id:
                if not h.get("entity_fuzzy") and hit_revision_id != doc.current_revision_id:
                    continue
            elif hit_revision_id:
                continue

        if category_id and item is not None and item.category_id != category_id:
            continue
        if item is not None and not _tags_match(item.tags, tag_filter):
            continue

        text = str(h.get("text") or "")
        if item is not None:
            title = item.title
            source_type = item.source_type
            page = item.page if item.page is not None else int(h.get("page") or 0)
            item_tags = [str(t) for t in (item.tags or [])]
            if not doc_id and item.document_id:
                doc_id = str(item.document_id)
            snippet = _snippet(text or item.content or item.summary or "")
        else:
            title = _snippet(text, 80) or "未命名片段"
            source_type = "document"
            page = int(h.get("page") or 0)
            item_tags = []
            snippet = _snippet(text)

        if not item_id:
            item_id = str(h.get("chunk_id") or "")

        cid = str(h.get("chunk_id") or "")
        semantic_score = _semantic_relevance_score(h, vector_score_by_id)
        if not _passes_relevance_gate(
            cid,
            bm25_ids=bm25_ids,
            bm25_score_by_id=bm25_score_by_id,
            vector_score_by_id=vector_score_by_id,
            rerank_applied=rerank_applied,
            h=h,
            embed_mode=embed_mode,
            allow_keyword_fallback=allow_keyword_fallback,
        ):
            continue

        results.append(
            HybridSearchHit(
                item_id=item_id,
                chunk_id=cid,
                doc_id=doc_id,
                title=title,
                text=text,
                snippet=snippet,
                score=semantic_score,
                source_type=source_type,
                page=page if page >= 0 else None,
                tags=item_tags,
            ),
        )
        if len(results) >= cap:
            break

    log.info(
        "hybrid_search kb=%s vec=%s bm25=%s fused=%s rerank=%s out=%s",
        kb_id,
        len(vector_hits),
        len(bm25_hits),
        len(fused),
        rerank_applied,
        len(results),
    )
    return results
