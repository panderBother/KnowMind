from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from whoosh import index as whoosh_index
from whoosh import qparser
from whoosh import query as wq
from whoosh.analysis import LowercaseFilter, RegexTokenizer
from whoosh.fields import ID, Schema, TEXT
from whoosh.writing import AsyncWriter

log = logging.getLogger(__name__)

# 中文按单字、英文/数字按词建立索引。Whoosh 默认 StandardAnalyzer 对中文长句
# 切分不稳定，导致实体名和短关键词很难命中。
_CONTENT_ANALYZER = RegexTokenizer(expression=r"[A-Za-z0-9_]+|[\u3400-\u9fff]") | LowercaseFilter()
_INDEX_VERSION = "main_v2_zh"

_schema = Schema(
    chunk_id=ID(stored=True, unique=True),
    kb_id=ID(stored=True),
    user_id=ID(stored=True),
    doc_id=ID(stored=True),
    revision_id=ID(stored=True),
    item_id=ID(stored=True),
    lifecycle_status=ID(stored=True),
    page=TEXT(stored=True),
    content=TEXT(stored=True, analyzer=_CONTENT_ANALYZER),
)

_REQUIRED_FIELDS = frozenset(_schema.names())


def _dir(root: str | Path) -> str:
    # 中文分析器升级使用新目录，保留旧索引，避免服务启动时删除大型索引文件。
    d = Path(root) / _INDEX_VERSION
    d.mkdir(parents=True, exist_ok=True)
    return str(d.resolve())


def _schema_matches(ix: whoosh_index.FileIndex) -> bool:
    if frozenset(ix.schema.names()) != _REQUIRED_FIELDS:
        return False
    # analyzer 变更时主动重建索引，避免继续使用旧的默认英文分词。
    return repr(ix.schema["content"].analyzer) == repr(_CONTENT_ANALYZER)


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


def _search_index(
    ix: whoosh_index.FileIndex,
    *,
    kb_id: str,
    query: str,
    top_k: int,
    lifecycle_status: str,
) -> list[dict[str, Any]]:
    parser = qparser.QueryParser("content", schema=ix.schema)
    try:
        text_q = parser.parse(query)
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
            hits = searcher.search(final_q, limit=top_k)
            if not hits:
                return []
            max_score = float(hits[0].score or 1.0) or 1.0
            for hit in hits:
                raw_score = float(hit.score or 0.0)
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
                        "score": max(0.0, min(1.0, raw_score / max_score)),
                        "bm25_score": raw_score,
                    },
                )
    except Exception as e:
        log.warning("whoosh search failed: %s", e)
        return []
    return out


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
    out = _search_index(
        ix,
        kb_id=kb_id,
        query=qtext,
        top_k=k,
        lifecycle_status=lifecycle_status,
    )

    # 中文分析器升级期间，旧 main 索引仍保留。合并旧索引结果，避免已有文档
    # 在完成重建前暂时失去 BM25 召回；同一 chunk 以新索引结果为准。
    legacy_path = Path(root) / "main"
    current_path = Path(_dir(root))
    if legacy_path.resolve() != current_path.resolve() and whoosh_index.exists_in(str(legacy_path)):
        try:
            legacy_ix = whoosh_index.open_dir(str(legacy_path))
            legacy_hits = _search_index(
                legacy_ix,
                kb_id=kb_id,
                query=qtext,
                top_k=k,
                lifecycle_status=lifecycle_status,
            )
            by_id = {str(row.get("chunk_id") or ""): row for row in out}
            for row in legacy_hits:
                cid = str(row.get("chunk_id") or "")
                if cid and cid not in by_id:
                    out.append(row)
            out.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
            out = out[:k]
            legacy_ix.close()
        except Exception as e:
            log.warning("legacy whoosh search failed: %s", e)

    log.info("whoosh search kb=%s hits=%s", kb_id, len(out))
    return out
