from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.db.sync_session import session_scope
from app.ingest.chunking import (
    TextChunk,
    chunk_settings_from_config,
    semantic_chunk_pages,
    semantic_chunk_text,
)
from app.ingest.document_state import DocumentStatus, transition_document
from app.ingest.embedding import embed_texts
from app.ingest.fingerprints import (
    metadata_hash,
    pipeline_fingerprint,
    text_hash,
    vector_fingerprint,
)
from app.ingest.registry import parse_file
from app.ingest.types import FileType, PageText
from app.indexing.vector_factory import get_vector_index
from app.indexing.whoosh_index import whoosh_list_chunk_ids_for_doc, whoosh_upsert_chunks
from app.models.orm import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeItem,
    new_uuid,
)
from app.services.item_indexing import (
    build_index_row,
    remove_index_chunks,
    remove_index_for_document,
)
from app.services.knowledge_category_service import get_or_create_default_category_sync
from app.storage.local import LocalBlobStorage
from app.utils.db_text import clamp_mediumtext
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

_ingest_serial_holder: list[threading.BoundedSemaphore | None] = [None]


def _ingest_serial_lock() -> threading.BoundedSemaphore:
    if _ingest_serial_holder[0] is None:
        _ingest_serial_holder[0] = threading.BoundedSemaphore(
            max(1, min(8, get_settings().ingest_max_parallel))
        )
    return _ingest_serial_holder[0]


def _set_progress(
    document_id: str,
    revision_id: str | None,
    progress: int,
    stage: str | None = None,
) -> None:
    with session_scope() as session:
        doc = session.get(Document, document_id)
        revision = session.get(DocumentRevision, revision_id) if revision_id else None
        value = max(0, min(100, progress))
        if revision is not None:
            revision.parse_progress = value
            if stage is not None:
                revision.parse_stage = (stage or "")[:64] or None
        if doc is not None and (not revision_id or doc.pending_revision_id == revision_id):
            doc.parse_progress = value
            if stage is not None:
                doc.parse_stage = (stage or "")[:64] or None


def _fail(document_id: str, revision_id: str | None, message: str) -> None:
    with session_scope() as session:
        doc = session.get(Document, document_id)
        revision = session.get(DocumentRevision, revision_id) if revision_id else None
        if revision is not None:
            revision.status = "failed"
            revision.parse_stage = "失败"
            revision.error_message = message[:4000]
        if doc is None:
            return
        if doc.pending_revision_id == revision_id:
            doc.pending_revision_id = None
        doc.error_message = message[:4000]
        doc.parse_stage = "版本更新失败" if doc.current_revision_id else "失败"
        if not doc.current_revision_id:
            transition_document(doc, DocumentStatus.FAILED, stage="失败", error=message)


def _reopen_pending(document_id: str, revision_id: str | None) -> None:
    try:
        with session_scope() as session:
            doc = session.get(Document, document_id)
            revision = session.get(DocumentRevision, revision_id) if revision_id else None
            if revision is not None and revision.status == "processing":
                revision.status = "pending"
                revision.parse_progress = 0
                revision.parse_stage = "重试排队中"
            if doc is not None and not doc.current_revision_id and doc.status == "processing":
                transition_document(doc, DocumentStatus.PENDING, progress=0, stage="重试排队中")
    except Exception:
        log.exception("ingest %s/%s: failed to reset pending", document_id, revision_id)


def _chunk_hash(text: str, page: int = 0) -> str:  # noqa: ARG001 - compatibility API
    """Chunk 语义内容 Hash；页码属于元数据，不应导致重新生成向量。"""
    return text_hash(text)


@dataclass(frozen=True)
class IncrementalChunkAssignment:
    chunk: TextChunk
    chunk_id: str
    content_hash: str
    metadata_hash: str
    vector_fingerprint: str
    requires_embedding: bool
    cached_vector: list[float] | None = None
    matched_chunk_id: str | None = None


def plan_incremental_chunks(
    *,
    document_id: str,
    new_chunks: list[TextChunk],
    old_chunks: list[DocumentChunk],
    revision_id: str | None = None,
    pipeline_hash: str | None = None,
) -> tuple[list[IncrementalChunkAssignment], list[str]]:
    """按文本 Hash 匹配；版本化调用生成新 ID，旧调用保持兼容复用 ID。"""
    reusable: dict[str, deque[DocumentChunk]] = defaultdict(deque)
    for old in old_chunks:
        reusable[old.content_hash].append(old)

    effective_pipeline = pipeline_hash or pipeline_fingerprint()
    assignments: list[IncrementalChunkAssignment] = []
    matched_old_ids: set[str] = set()
    hash_occurrences: dict[str, int] = defaultdict(int)
    for ordinal, chunk in enumerate(new_chunks):
        content = _chunk_hash(chunk.text, chunk.page)
        matched = reusable[content].popleft() if reusable[content] else None
        occurrence = hash_occurrences[content]
        hash_occurrences[content] += 1
        if revision_id:
            chunk_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"knowmind:{document_id}:{revision_id}:{content}:{occurrence}",
                )
            )
        elif matched is not None:
            chunk_id = matched.chunk_id
        else:
            chunk_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"knowmind:{document_id}:{content}:{occurrence}",
                )
            )
        if matched is not None:
            matched_old_ids.add(matched.chunk_id)
        fingerprint = vector_fingerprint(content, effective_pipeline)
        cached = None
        if matched is not None and matched.vector_fingerprint == fingerprint and matched.embedding:
            cached = [float(value) for value in matched.embedding]
        assignments.append(
            IncrementalChunkAssignment(
                chunk=chunk,
                chunk_id=chunk_id,
                content_hash=content,
                metadata_hash=metadata_hash(page=chunk.page, ordinal=ordinal),
                vector_fingerprint=fingerprint,
                requires_embedding=matched is None,
                cached_vector=cached,
                matched_chunk_id=matched.chunk_id if matched is not None else None,
            )
        )

    removed_ids = [old.chunk_id for old in old_chunks if old.chunk_id not in matched_old_ids]
    return assignments, removed_ids


def _extract_full_text(
    *,
    file_path: str,
    filename: str,
    file_type_str: str | None,
    parsed_content: str | None,
    parsed_title: str | None,
    doc_title: str | None,
) -> tuple[str, list[PageText], str | None]:
    if (parsed_content or "").strip():
        body = parsed_content.strip()
        return body, [PageText(page_index=0, text=body)], parsed_title or doc_title

    file_type = FileType(file_type_str) if file_type_str else FileType.PDF
    if file_type == FileType.PDF:
        from app.ingest.pdf import extract_pdf_pages

        pages = extract_pdf_pages(file_path)
        merged = "\n\n".join((p.text or "").strip() for p in pages if (p.text or "").strip())
        return merged, pages, None

    result = parse_file(file_path, filename, file_type)
    pages = result.pages if result.pages else [PageText(page_index=0, text=result.merged_content())]
    return result.merged_content(), pages, result.title


def _semantic_index_chunks(*, full_text: str, pages: list[PageText]) -> list[TextChunk]:
    min_chars, max_chars, overlap = chunk_settings_from_config()
    if pages and len(pages) > 1:
        return semantic_chunk_pages(
            pages,
            max_chars=max_chars,
            min_chars=min_chars,
            overlap=overlap,
        )
    return semantic_chunk_text(
        full_text,
        max_chars=max_chars,
        min_chars=min_chars,
        overlap=overlap,
    )


def _embed_with_progress(
    document_id: str,
    revision_id: str,
    texts: list[str],
) -> list[list[float]]:
    if not texts:
        return []
    settings = get_settings()
    batch_size = max(1, settings.embedding_batch_size)
    vectors: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(embed_texts(batch))
        done = min(start + len(batch), total)
        pct = 50 + int(30 * done / total)
        _set_progress(document_id, revision_id, pct, f"向量化 {done}/{total}")
    return vectors


def _legacy_revision(session, doc: Document) -> DocumentRevision:
    revision = DocumentRevision(
        id=new_uuid(),
        document_id=doc.id,
        created_by=doc.user_id,
        revision_no=1,
        filename=doc.filename,
        file_type=doc.file_type,
        storage_key=doc.storage_key,
        file_bytes=doc.file_bytes,
        source_sha256=doc.sha256
        or hashlib.sha256((doc.md5 or doc.storage_key).encode("utf-8")).hexdigest(),
        pipeline_fingerprint=pipeline_fingerprint(),
        parsed_title=doc.parsed_title,
        parsed_summary=doc.parsed_summary,
        parsed_content=doc.parsed_content,
        status=doc.status,
        parse_progress=doc.parse_progress,
        parse_stage=doc.parse_stage,
    )
    session.add(revision)
    session.flush()
    doc.pending_revision_id = revision.id
    for chunk in session.scalars(
        select(DocumentChunk).where(
            DocumentChunk.document_id == doc.id,
            DocumentChunk.revision_id.is_(None),
        )
    ).all():
        chunk.revision_id = revision.id
    return revision


def process_document_once(document_id: str, revision_id: str | None = None) -> bool:
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None or doc.lifecycle_status != "active":
            log.error("ingest %s: active document not found", document_id)
            return False
        revision = session.get(DocumentRevision, revision_id) if revision_id else None
        if revision is None:
            candidate_id = doc.pending_revision_id or doc.current_revision_id
            revision = session.get(DocumentRevision, candidate_id) if candidate_id else None
        if revision is None:
            revision = _legacy_revision(session, doc)
        revision_id = revision.id
        if revision.document_id != doc.id:
            return False
        if revision.status == "active" and doc.current_revision_id == revision.id:
            return True
        if revision.status not in ("pending", "processing"):
            log.warning("ingest %s/%s: status=%s, skip", document_id, revision.id, revision.status)
            return False
        revision.status = "processing"
        revision.parse_progress = 5
        revision.parse_stage = "开始解析"
        revision.error_message = None
        if not doc.current_revision_id and doc.status == "pending":
            transition_document(doc, DocumentStatus.PROCESSING, progress=5, stage="开始解析")
        doc.pending_revision_id = revision.id
        doc.parse_progress = 5
        doc.parse_stage = f"v{revision.revision_no} 开始解析"
        doc.error_message = None

        kb_id = doc.kb_id
        user_id = doc.user_id
        old_revision_id = doc.current_revision_id
        old_chunks = list(
            session.scalars(
                select(DocumentChunk)
                .where(
                    DocumentChunk.document_id == doc.id,
                    DocumentChunk.revision_id == old_revision_id,
                )
                .order_by(DocumentChunk.ordinal.asc())
            ).all()
        )
        if old_revision_id is None:
            old_chunks = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == doc.id)
                    .order_by(DocumentChunk.ordinal.asc())
                ).all()
            )
        old_item = session.scalar(select(KnowledgeItem).where(KnowledgeItem.document_id == doc.id))
        old_item_id = old_item.id if old_item is not None else None
        storage_key = revision.storage_key
        filename = revision.filename
        file_type_str = revision.file_type
        parsed_title = revision.parsed_title
        parsed_summary = revision.parsed_summary
        parsed_content = revision.parsed_content
        revision_no = revision.revision_no
        revision_pipeline = revision.pipeline_fingerprint

    settings = get_settings()
    storage = LocalBlobStorage(settings.storage_local_root)
    file_path = storage.filesystem_path(storage_key)
    _set_progress(document_id, revision_id, 15, "提取文本")
    full_text, pages, parse_title = _extract_full_text(
        file_path=file_path,
        filename=filename,
        file_type_str=file_type_str,
        parsed_content=parsed_content,
        parsed_title=parsed_title,
        doc_title=None,
    )
    if not (full_text or "").strip():
        _fail(document_id, revision_id, "未提取到文本内容")
        return False

    index_chunks = _semantic_index_chunks(full_text=full_text, pages=pages)
    _set_progress(document_id, revision_id, 42, f"语义切块 {len(index_chunks)} 段")
    assignments, removed_ids = plan_incremental_chunks(
        document_id=document_id,
        revision_id=revision_id,
        pipeline_hash=revision_pipeline,
        new_chunks=index_chunks,
        old_chunks=old_chunks,
    )

    needs_vector = [entry for entry in assignments if entry.cached_vector is None]
    fresh_vectors = _embed_with_progress(
        document_id,
        revision_id,
        [entry.chunk.text for entry in needs_vector],
    )
    vectors_by_id = {
        entry.chunk_id: vector for entry, vector in zip(needs_vector, fresh_vectors, strict=True)
    }
    item_id = old_item_id or new_uuid()
    rows: list[dict] = []
    for entry in assignments:
        vector = entry.cached_vector or vectors_by_id[entry.chunk_id]
        rows.append(
            build_index_row(
                chunk_id=entry.chunk_id,
                kb_id=kb_id,
                user_id=user_id,
                doc_id=document_id,
                revision_id=revision_id,
                item_id=item_id,
                page=entry.chunk.page,
                text=entry.chunk.text,
                vector=vector,
                lifecycle_status="staging",
            )
        )

    _set_progress(document_id, revision_id, 84, "写入暂存索引")
    if rows:
        vector_index = get_vector_index()
        vector_index.upsert_chunks(rows)
        whoosh_upsert_chunks(settings.whoosh_index_root, rows)
        published_rows = [{**row, "lifecycle_status": "published"} for row in rows]
        vector_index.upsert_chunks(published_rows)
        whoosh_upsert_chunks(settings.whoosh_index_root, published_rows)
        expected_ids = {str(row["chunk_id"]) for row in rows}
        invalid_dimensions = [
            str(row["chunk_id"])
            for row in rows
            if len(row["vector"]) != settings.embedding_vector_dim
        ]
        if invalid_dimensions:
            raise RuntimeError(f"向量维度校验失败：{len(invalid_dimensions)} 个 Chunk")
        vector_list = getattr(vector_index, "list_chunk_ids_for_doc", None)
        if not callable(vector_list):
            raise RuntimeError("向量索引不支持激活前完整性校验")
        vector_ids = set(vector_list(document_id))
        keyword_ids = set(whoosh_list_chunk_ids_for_doc(settings.whoosh_index_root, document_id))
        missing_vector = expected_ids - vector_ids
        missing_keyword = expected_ids - keyword_ids
        if missing_vector or missing_keyword:
            raise RuntimeError(
                "索引完整性校验失败："
                f"向量缺失 {len(missing_vector)}，全文缺失 {len(missing_keyword)}"
            )
    _set_progress(document_id, revision_id, 94, "校验并激活")

    title = parsed_title or parse_title
    if not title and full_text:
        title = (full_text.split("\n")[0] or "").strip()[:512] or None
    base_name = (title or filename or "document").rsplit(".", 1)[0][:80]
    first_chunk_id = assignments[0].chunk_id if assignments else None
    now = datetime.now(UTC)
    matched_count = sum(1 for entry in assignments if entry.matched_chunk_id)
    unmatched_count = len(assignments) - matched_count
    changed_count = min(unmatched_count, len(removed_ids))
    added_count = max(0, unmatched_count - changed_count)

    with session_scope() as session:
        doc = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
        revision = session.get(DocumentRevision, revision_id)
        if doc is None or revision is None or doc.pending_revision_id != revision_id:
            raise RuntimeError("文档版本已被其它任务替换，拒绝激活")
        previous_revision_id = doc.current_revision_id
        previous = (
            session.get(DocumentRevision, previous_revision_id) if previous_revision_id else None
        )
        if previous is not None and previous.id != revision.id:
            previous.status = "superseded"

        existing_revision_chunks = list(
            session.scalars(
                select(DocumentChunk).where(DocumentChunk.revision_id == revision_id)
            ).all()
        )
        for existing in existing_revision_chunks:
            session.delete(existing)
        session.flush()
        for ordinal, (entry, row) in enumerate(zip(assignments, rows, strict=True)):
            session.add(
                DocumentChunk(
                    document_id=document_id,
                    revision_id=revision_id,
                    chunk_id=entry.chunk_id,
                    content_hash=entry.content_hash,
                    metadata_hash=entry.metadata_hash,
                    vector_fingerprint=entry.vector_fingerprint,
                    embedding=row["vector"],
                    lifecycle_status="published",
                    ordinal=ordinal,
                    page=entry.chunk.page,
                    text=entry.chunk.text,
                )
            )

        revision.status = "active"
        revision.parse_progress = 100
        revision.parse_stage = "完成"
        revision.error_message = None
        revision.extracted_text_sha256 = text_hash(full_text)
        revision.parsed_content = clamp_mediumtext(full_text)
        revision.parsed_title = title
        revision.parsed_summary = parsed_summary
        revision.chunk_count = len(assignments)
        revision.added_chunk_count = added_count
        revision.changed_chunk_count = changed_count
        revision.reused_chunk_count = matched_count
        revision.removed_chunk_count = len(removed_ids)
        revision.activated_at = now

        was_initial = previous_revision_id is None and old_item_id is None
        doc.current_revision_id = revision.id
        doc.pending_revision_id = None
        doc.filename = revision.filename
        doc.file_type = revision.file_type
        doc.storage_key = revision.storage_key
        doc.file_bytes = revision.file_bytes
        doc.sha256 = revision.source_sha256
        doc.status = "done"
        doc.chunk_count = len(assignments)
        doc.title = title
        doc.parsed_title = title
        doc.parsed_summary = parsed_summary
        doc.parsed_content = clamp_mediumtext(full_text)
        doc.parse_progress = 100
        doc.parse_stage = f"v{revision_no} 已激活"
        doc.error_message = None

        default_category_id = get_or_create_default_category_sync(session, kb_id, user_id)
        item_record = session.get(KnowledgeItem, item_id) if old_item_id else None
        if item_record is None:
            item_record = KnowledgeItem(
                id=item_id,
                kb_id=kb_id,
                user_id=user_id,
                document_id=document_id,
                category_id=default_category_id,
                source_type="document",
                lifecycle_status="published",
                access_level="internal",
                page=0,
                published_at=now,
            )
            session.add(item_record)
        item_record.title = base_name[:200]
        item_record.content = clamp_mediumtext(full_text) or full_text[:8000]
        item_record.summary = (parsed_summary or title or base_name)[:500]
        item_record.source = filename
        item_record.chunk_id = first_chunk_id
        item_record.lifecycle_status = "published"

        if previous_revision_id and previous_revision_id != revision.id:
            for chunk in session.scalars(
                select(DocumentChunk).where(DocumentChunk.revision_id == previous_revision_id)
            ).all():
                chunk.lifecycle_status = "superseded"
        kb = session.get(KnowledgeBase, kb_id)
        if kb is not None and was_initial:
            kb.doc_count = int(kb.doc_count or 0) + 1

    old_ids = [chunk.chunk_id for chunk in old_chunks]
    if old_ids:
        remove_index_chunks(old_ids)
    log.info(
        "ingest activated doc=%s revision=%s total=%s added=%s changed=%s reused=%s removed=%s",
        document_id,
        revision_id,
        len(assignments),
        added_count,
        changed_count,
        matched_count,
        len(removed_ids),
    )
    return True


def run_document_ingest(document_id: str, revision_id: str | None = None) -> None:
    log.info("ingest job start doc=%s revision=%s", document_id, revision_id)
    with _ingest_serial_lock():
        last_err: str | None = None
        for attempt in range(3):
            try:
                if process_document_once(document_id, revision_id):
                    return
                last_err = "ingest incomplete"
            except Exception as exc:  # noqa: BLE001
                last_err = repr(exc)
                log.exception(
                    "document %s revision %s attempt %s failed", document_id, revision_id, attempt
                )
                _reopen_pending(document_id, revision_id)
        _fail(document_id, revision_id, last_err or "unknown error")


def reconcile_all_document_indexes_once() -> dict[str, int]:
    """周期对账：当前版本为事实源，补缺失、删孤儿，并恢复超时任务。"""
    settings = get_settings()
    snapshots: list[tuple[Document, list[DocumentChunk], KnowledgeItem | None]] = []
    deleted_document_ids: list[str] = []
    recoveries: list[tuple[str, str]] = []
    cutoff = (
        datetime.now(UTC) - timedelta(seconds=settings.ingest_processing_timeout_seconds)
    ).replace(tzinfo=None)

    with session_scope() as session:
        docs = list(session.scalars(select(Document)).all())
        for doc in docs:
            if doc.lifecycle_status != "active":
                deleted_document_ids.append(doc.id)
                continue
            if doc.current_revision_id:
                chunks = list(
                    session.scalars(
                        select(DocumentChunk)
                        .where(
                            DocumentChunk.document_id == doc.id,
                            DocumentChunk.revision_id == doc.current_revision_id,
                            DocumentChunk.lifecycle_status == "published",
                        )
                        .order_by(DocumentChunk.ordinal.asc())
                    ).all()
                )
                item = session.scalar(
                    select(KnowledgeItem).where(
                        KnowledgeItem.document_id == doc.id,
                        KnowledgeItem.lifecycle_status == "published",
                    )
                )
                snapshots.append((doc, chunks, item))
            if doc.pending_revision_id:
                revision = session.get(DocumentRevision, doc.pending_revision_id)
                stale_revision_id = session.scalar(
                    select(DocumentRevision.id).where(
                        DocumentRevision.id == doc.pending_revision_id,
                        DocumentRevision.status.in_(("pending", "processing")),
                        DocumentRevision.updated_at < cutoff,
                    )
                )
                if revision is not None and stale_revision_id is not None:
                    revision.status = "pending"
                    revision.parse_progress = 0
                    revision.parse_stage = "超时恢复排队中"
                    doc.parse_progress = 0
                    doc.parse_stage = f"v{revision.revision_no} 超时恢复排队中"
                    recoveries.append((doc.id, revision.id))

    report = {
        "documents": len(snapshots),
        "repaired_chunks": 0,
        "removed_orphans": 0,
        "recovered_jobs": len(recoveries),
        "errors": 0,
    }
    for document_id in deleted_document_ids:
        try:
            remove_index_for_document(document_id)
        except Exception:
            report["errors"] += 1
            log.exception("reconcile cleanup failed doc=%s", document_id)

    for doc, chunks, item in snapshots:
        try:
            vector_index = get_vector_index()
            vector_list = getattr(vector_index, "list_chunk_ids_for_doc", None)
            if not callable(vector_list):
                raise RuntimeError("向量索引不支持按文档列出 Chunk")
            vector_ids = set(vector_list(doc.id))
            keyword_ids = set(whoosh_list_chunk_ids_for_doc(settings.whoosh_index_root, doc.id))
            expected_ids = {chunk.chunk_id for chunk in chunks}
            orphan_ids = sorted((vector_ids | keyword_ids) - expected_ids)
            if orphan_ids:
                remove_index_chunks(orphan_ids)
                report["removed_orphans"] += len(orphan_ids)

            missing_ids = expected_ids - (vector_ids & keyword_ids)
            repair_rows: list[dict] = []
            for chunk in chunks:
                if chunk.chunk_id not in missing_ids or not chunk.embedding or item is None:
                    continue
                repair_rows.append(
                    build_index_row(
                        chunk_id=chunk.chunk_id,
                        kb_id=doc.kb_id,
                        user_id=doc.user_id,
                        doc_id=doc.id,
                        revision_id=doc.current_revision_id,
                        item_id=item.id,
                        page=chunk.page,
                        text=chunk.text,
                        vector=[float(value) for value in chunk.embedding],
                        lifecycle_status="published",
                    )
                )
            if repair_rows:
                vector_index.upsert_chunks(repair_rows)
                whoosh_upsert_chunks(settings.whoosh_index_root, repair_rows)
                report["repaired_chunks"] += len(repair_rows)
        except Exception:
            report["errors"] += 1
            log.exception("reconcile indexes failed doc=%s", doc.id)

    for document_id, revision_id in recoveries:
        process_document_task.delay(document_id, revision_id)
    log.info("document index reconciliation complete: %s", report)
    return report


@celery_app.task(name="documents.process_document")
def process_document_task(document_id: str, revision_id: str | None = None) -> None:
    run_document_ingest(document_id, revision_id)


@celery_app.task(name="documents.reconcile_indexes")
def reconcile_document_indexes_task() -> dict[str, int]:
    return reconcile_all_document_indexes_once()
