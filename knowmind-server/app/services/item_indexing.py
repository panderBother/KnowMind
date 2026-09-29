"""知识条目向量/全文索引写入与删除。"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import get_settings
from app.indexing.vector_factory import get_vector_index
from app.indexing.whoosh_index import (
    open_or_create_index,
    whoosh_delete_chunk,
    whoosh_delete_chunks_for_doc,
    whoosh_upsert_chunks,
)
from app.ingest.chunking import (
    TextChunk,
    chunk_settings_from_config,
    chunk_token_settings_from_config,
    semantic_chunk_text,
)
from app.ingest.embedding import embed_texts
from whoosh.writing import AsyncWriter

log = logging.getLogger(__name__)


def normalize_index_text(title: str | None, text: str) -> str:
    """写入检索索引的正文：去 LLM 代码块包裹，并前置标题便于关键词命中。"""
    body = (text or "").strip()
    body = re.sub(r"^```[\w-]*\s*", "", body)
    body = re.sub(r"\s*```$", "", body).strip()
    t = (title or "").strip()
    if t and t not in body[: max(len(t) + 20, 80)]:
        return f"{t}\n\n{body}".strip() if body else t
    return body or t or " "


def build_index_row(
    *,
    chunk_id: str,
    kb_id: str,
    user_id: str,
    doc_id: str | None,
    item_id: str,
    page: int,
    text: str,
    vector: list[float],
    lifecycle_status: str,
    revision_id: str | None = None,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "kb_id": kb_id,
        "user_id": user_id,
        "doc_id": doc_id or "",
        "revision_id": revision_id or "",
        "item_id": item_id,
        "page": page,
        "text": text,
        "vector": vector,
        "lifecycle_status": lifecycle_status,
    }


def upsert_index_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    get_vector_index().upsert_chunks(rows)
    whoosh_upsert_chunks(get_settings().whoosh_index_root, rows)


def _list_chunk_ids_for_doc(doc_id: str) -> list[str]:
    if not doc_id:
        return []
    idx = get_vector_index()
    list_fn = getattr(idx, "list_chunk_ids_for_doc", None)
    if callable(list_fn):
        return list_fn(doc_id)
    return []


def remove_index_for_document(doc_id: str) -> None:
    """删除某文档关联的全部检索段（向量 + Whoosh）。"""
    if not doc_id:
        return
    idx = get_vector_index()
    delete_by_doc = getattr(idx, "delete_chunks_for_doc", None)
    if callable(delete_by_doc):
        delete_by_doc(doc_id)
    else:
        chunk_ids = _list_chunk_ids_for_doc(doc_id)
        if chunk_ids:
            idx.delete_chunks(chunk_ids)
    whoosh_delete_chunks_for_doc(get_settings().whoosh_index_root, doc_id)


def remove_index_for_item(item_id: str, *, chunk_id: str | None = None) -> None:
    """删除某条目关联的全部检索段（含语义切分的多段）。"""
    if not item_id and not chunk_id:
        return
    idx = get_vector_index()
    if item_id:
        delete_by_item = getattr(idx, "delete_chunks_for_item", None)
        if callable(delete_by_item):
            delete_by_item(item_id)
        elif chunk_id:
            remove_index_chunk(chunk_id)
    elif chunk_id:
        remove_index_chunk(chunk_id)
    root = get_settings().whoosh_index_root
    if item_id:
        ix = open_or_create_index(root)
        writer = AsyncWriter(ix)
        writer.delete_by_term("item_id", item_id)
        writer.commit()


def remove_index_chunk(chunk_id: str | None) -> None:
    if not chunk_id:
        return
    remove_index_chunks([chunk_id])


def remove_index_chunks(chunk_ids: list[str]) -> None:
    ids = [c for c in chunk_ids if c]
    if not ids:
        return
    get_vector_index().delete_chunks(ids)
    root = get_settings().whoosh_index_root
    for cid in ids:
        whoosh_delete_chunk(root, cid)


def index_text_item(
    *,
    chunk_id: str,
    kb_id: str,
    user_id: str,
    item_id: str,
    text: str,
    lifecycle_status: str,
    doc_id: str | None = None,
    page: int = 0,
    title: str | None = None,
) -> str | None:
    """单段正文索引（手动条目等）；长文会语义切为多段、同一 item_id。返回首个 chunk_id。"""
    if doc_id:
        remove_index_for_document(doc_id)
    else:
        remove_index_for_item(item_id, chunk_id=chunk_id)
    rows, first_id = _build_rows_for_text(
        text=text,
        title=title,
        kb_id=kb_id,
        user_id=user_id,
        item_id=item_id,
        doc_id=doc_id,
        lifecycle_status=lifecycle_status,
        page=page,
    )
    upsert_index_rows(rows)
    return first_id


def reindex_document_item(
    *,
    kb_id: str,
    user_id: str,
    doc_id: str,
    item_id: str,
    text: str,
    lifecycle_status: str = "published",
    title: str | None = None,
) -> str | None:
    """
    文档条目：语义切分后写入索引，返回首个 chunk_id（兼容 item.chunk_id 字段）。
    """
    remove_index_for_document(doc_id)
    rows, first_id = _build_rows_for_text(
        text=text,
        title=title,
        kb_id=kb_id,
        user_id=user_id,
        item_id=item_id,
        doc_id=doc_id,
        lifecycle_status=lifecycle_status,
        page=0,
    )
    upsert_index_rows(rows)
    return first_id


def _build_rows_for_text(
    *,
    text: str,
    kb_id: str,
    user_id: str,
    item_id: str,
    doc_id: str | None,
    lifecycle_status: str,
    page: int,
    title: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    from app.models.orm import new_uuid

    min_chars, max_chars, overlap = chunk_settings_from_config()
    target_tokens, max_tokens, overlap_tokens = chunk_token_settings_from_config()
    body = normalize_index_text(title, text)
    chunks: list[TextChunk] = semantic_chunk_text(
        body,
        page=page,
        max_chars=max_chars,
        min_chars=min_chars,
        overlap=overlap,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )
    if not chunks:
        chunks = [TextChunk(text=body, page=page)]
    texts = [c.text for c in chunks]
    vectors = embed_texts(texts)
    rows: list[dict[str, Any]] = []
    first_id: str | None = None
    for ch, vec in zip(chunks, vectors, strict=True):
        cid = new_uuid()
        if first_id is None:
            first_id = cid
        rows.append(
            build_index_row(
                chunk_id=cid,
                kb_id=kb_id,
                user_id=user_id,
                doc_id=doc_id,
                item_id=item_id,
                page=ch.page,
                text=ch.text,
                vector=vec,
                lifecycle_status=lifecycle_status,
            )
        )
    return rows, first_id
