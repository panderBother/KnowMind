from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from whoosh import index as whoosh_index
from whoosh import qparser
from whoosh import query as wq
from whoosh.fields import ID, Schema, TEXT
from whoosh.writing import AsyncWriter

log = logging.getLogger(__name__)

_schema = Schema(
    chunk_id=ID(stored=True, unique=True),
    kb_id=ID(stored=True),
    user_id=ID(stored=True),
    doc_id=ID(stored=True),
    revision_id=ID(stored=True),
    item_id=ID(stored=True),
    lifecycle_status=ID(stored=True),
    page=TEXT(stored=True),
    content=TEXT(stored=True),
)

_REQUIRED_FIELDS = frozenset(_schema.names())


def _dir(root: str | Path) -> str:
    d = Path(root) / "main"
    d.mkdir(parents=True, exist_ok=True)
    return str(d.resolve())


def _schema_matches(ix: whoosh_index.FileIndex) -> bool:
    return frozenset(ix.schema.names()) == _REQUIRED_FIELDS


def _recreate_index(root: str | Path) -> whoosh_index.FileIndex:
    path = Path(_dir(root))
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    log.warning("whoosh index rebuilt at %s (schema upgrade)", path)
    return whoosh_index.create_in(str(path), _schema)


def open_or_create_index(root: str | Path) -> whoosh_index.FileIndex:
    path = _dir(root)
    if whoosh_index.exists_in(path):
        ix = whoosh_index.open_dir(path)
        if not _schema_matches(ix):
            log.warning(
                "whoosh schema mismatch existing=%s required=%s",
                sorted(ix.schema.names()),
                sorted(_REQUIRED_FIELDS),
            )
            ix.close()
            return _recreate_index(root)
        return ix
    return whoosh_index.create_in(path, _schema)


def whoosh_upsert_chunks(root: str | Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    def _write(ix: whoosh_index.FileIndex) -> None:
        writer = AsyncWriter(ix)
        for r in rows:
            writer.update_document(
                chunk_id=r["chunk_id"],
                kb_id=r["kb_id"],
                user_id=r["user_id"],
                doc_id=str(r.get("doc_id") or ""),
                revision_id=str(r.get("revision_id") or ""),
                item_id=str(r.get("item_id") or ""),
                lifecycle_status=str(r.get("lifecycle_status") or "published"),
                page=str(int(r["page"])),
                content=str(r["text"])[:200000],
            )
        writer.commit()

    ix = open_or_create_index(root)
    try:
        _write(ix)
    except Exception as e:
        from whoosh.fields import UnknownFieldError

        if not isinstance(e, UnknownFieldError):
            raise
        log.warning("whoosh upsert hit UnknownFieldError, rebuilding once: %s", e)
        ix.close()
        ix = _recreate_index(root)
        _write(ix)
    log.info("whoosh upsert %s chunks", len(rows))


def whoosh_delete_chunk(root: str | Path, chunk_id: str) -> None:
    if not chunk_id:
        return
    ix = open_or_create_index(root)
    writer = AsyncWriter(ix)
    writer.delete_by_term("chunk_id", chunk_id)
    writer.commit()


def whoosh_delete_chunks(root: str | Path, chunk_ids: list[str]) -> None:
    for cid in chunk_ids:
        if cid:
            whoosh_delete_chunk(root, cid)


def whoosh_delete_chunks_for_doc(root: str | Path, doc_id: str) -> None:
    if not doc_id:
        return
    ix = open_or_create_index(root)
    writer = AsyncWriter(ix)
    writer.delete_by_term("doc_id", doc_id)
    writer.commit()


def whoosh_list_chunk_ids_for_doc(root: str | Path, doc_id: str) -> list[str]:
    if not doc_id:
        return []
    ix = open_or_create_index(root)
    with ix.searcher() as searcher:
        return [
            str(hit.get("chunk_id") or "")
            for hit in searcher.search(wq.Term("doc_id", doc_id), limit=None)
            if hit.get("chunk_id")
        ]


def whoosh_search(
    root: str | Path,
    *,
    kb_id: str,
    query: str,
    top_k: int = 20,
    lifecycle_status: str = "published",
) -> list[dict[str, Any]]:
    """BM25 关键词检索；仅返回指定 kb 与 lifecycle 的 chunk。"""
    qtext = (query or "").strip()
    if not qtext or not kb_id:
        return []

    k = max(1, min(int(top_k), 64))
    ix = open_or_create_index(root)
    parser = qparser.QueryParser("content", schema=ix.schema)
    try:
        text_q = parser.parse(qtext)
    except Exception as e:
        log.warning("whoosh parse query failed: %s", e)
        return []

    filter_q = wq.And(
        [
            wq.Term("kb_id", kb_id),
            wq.Term("lifecycle_status", lifecycle_status),
        ],
    )
    final_q = wq.And([text_q, filter_q])

    out: list[dict[str, Any]] = []
    try:
        with ix.searcher() as searcher:
            hits = searcher.search(final_q, limit=k)
            if not hits:
                return []
            max_score = float(hits[0].score or 1.0) or 1.0
            for hit in hits:
                raw_score = float(hit.score or 0.0)
                norm = raw_score / max_score if max_score > 0 else 0.0
                page_raw = hit.get("page") or "0"
                try:
                    page = int(page_raw)
                except (TypeError, ValueError):
                    page = 0
                out.append(
                    {
                        "chunk_id": str(hit.get("chunk_id") or ""),
                        "text": str(hit.get("content") or ""),
                        "doc_id": str(hit.get("doc_id") or ""),
                        "revision_id": str(hit.get("revision_id") or ""),
                        "item_id": str(hit.get("item_id") or ""),
                        "page": page,
                        "score": max(0.0, min(1.0, norm)),
                        "bm25_score": raw_score,
                    },
                )
    except Exception as e:
        log.warning("whoosh search failed: %s", e)
        return []

    log.info("whoosh search kb=%s hits=%s", kb_id, len(out))
    return out
