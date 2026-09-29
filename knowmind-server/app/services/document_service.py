from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.ingest.registry import detect_file_type, parse_file, requires_preview
from app.ingest.document_state import DocumentStatus, transition_document
from app.ingest.fingerprints import pipeline_fingerprint, sha256_bytes
from app.ingest.types import SUPPORTED_EXTENSIONS, FileType
from app.models.orm import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeItem,
    new_uuid,
)
from app.schemas.document import (
    DocumentConfirmImportResponse,
    DocumentIndexReconcileResponse,
    DocumentOut,
    DocumentParsedContentOut,
    DocumentParsedContentUpdate,
    DocumentRevisionOut,
    DocumentUploadResponse,
    DocumentVersionUploadResponse,
)
from app.services import item_indexing
from app.storage import get_blob_storage
from app.utils.db_text import clamp_mediumtext

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"


def _safe_filename(name: str | None, fallback: str = "upload.bin") -> str:
    if not name:
        return fallback
    base = Path(name).name
    base = re.sub(r"[^\w.\-()\s\u4e00-\u9fff]", "_", base, flags=re.UNICODE).strip()
    return base or fallback


def _validate_file_magic(data: bytes, file_type: FileType, filename: str) -> None:
    ext = Path(filename).suffix.lower()
    if file_type == FileType.PDF and not data.startswith(PDF_MAGIC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不是有效 PDF：{filename}")
    if file_type in (FileType.DOCX, FileType.XLSX) and not data.startswith(ZIP_MAGIC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不是有效 {ext} 文件：{filename}")
    if file_type == FileType.DOC and not (
        data.startswith(b"\xd0\xcf\x11\xe0") or data[:4] == ZIP_MAGIC
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不是有效 Word 文件：{filename}")


def _mime_for_type(file_type: FileType) -> str:
    from app.ingest.types import MIME_BY_TYPE

    return MIME_BY_TYPE.get(file_type, "application/octet-stream")


async def _ensure_kb(session: AsyncSession, user_id: str, kb_id: str) -> KnowledgeBase:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    return kb


def _parse_sync(path: str, filename: str, file_type: FileType):
    return parse_file(path, filename, file_type)


async def upload_documents(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    files: list[UploadFile],
    background_tasks: BackgroundTasks,
) -> DocumentUploadResponse:
    s = get_settings()
    if len(files) > s.pdf_max_batch:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"单次最多上传 {s.pdf_max_batch} 个文件",
        )
    await _ensure_kb(session, user_id, kb_id)

    max_bytes = s.pdf_max_upload_mb * 1024 * 1024
    storage = get_blob_storage()
    created: list[Document] = []
    created_revision_ids: dict[str, str] = {}
    skipped = 0
    needs_preview: list[str] = []

    for up in files:
        raw_name = _safe_filename(up.filename)
        ext = Path(raw_name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"不支持的格式「{ext or raw_name}」。支持：{supported}",
            )
        file_type = detect_file_type(raw_name)
        data = await up.read()
        if len(data) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"文件过大（上限 {s.pdf_max_upload_mb}MB）：{raw_name}",
            )
        _validate_file_magic(data, file_type, raw_name)

        md5_hex = hashlib.md5(data, usedforsecurity=False).hexdigest()
        sha256_hex = sha256_bytes(data)
        dup_result = await session.execute(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.lifecycle_status == "active",
                Document.sha256 == sha256_hex,
                Document.status != "failed",
            )
        )
        duplicate = dup_result.scalars().first()
        if duplicate is None:
            legacy_result = await session.execute(
                select(Document).where(
                    Document.kb_id == kb_id,
                    Document.lifecycle_status == "active",
                    Document.sha256.is_(None),
                    Document.md5 == md5_hex,
                    Document.status != "failed",
                )
            )
            for legacy in legacy_result.scalars().all():
                try:
                    verified_hash = await asyncio.to_thread(
                        _sha256_file,
                        document_filesystem_path(legacy),
                    )
                except (OSError, ValueError):
                    continue
                legacy.sha256 = verified_hash
                if verified_hash == sha256_hex:
                    duplicate = legacy
                    break
        if duplicate is not None:
            skipped += 1
            continue

        doc_id = new_uuid()
        revision_id = new_uuid()
        key = f"users/{user_id}/kb/{kb_id}/docs/{doc_id}/revisions/{revision_id}/{raw_name}"
        await storage.put_bytes(key, data)

        doc = Document(
            id=doc_id,
            kb_id=kb_id,
            user_id=user_id,
            filename=raw_name,
            file_type=file_type.value,
            storage_key=key,
            status="pending",
            file_bytes=len(data),
            md5=md5_hex,
            sha256=sha256_hex,
            pending_revision_id=revision_id,
        )
        revision = DocumentRevision(
            id=revision_id,
            document_id=doc_id,
            created_by=user_id,
            revision_no=1,
            filename=raw_name,
            file_type=file_type.value,
            storage_key=key,
            file_bytes=len(data),
            source_sha256=sha256_hex,
            pipeline_fingerprint=pipeline_fingerprint(),
            status="pending",
        )

        if requires_preview(file_type):
            doc.status = "preview"
            doc.parse_stage = "解析预览"
            revision.status = "preview"
            revision.parse_stage = "解析预览"
            try:
                fspath = storage.filesystem_path(key)
                result = await asyncio.to_thread(_parse_sync, fspath, raw_name, file_type)
                doc.parsed_content = clamp_mediumtext(result.merged_content())
                doc.parsed_title = result.title
                doc.parsed_summary = result.summary
                doc.title = result.title
                doc.parse_progress = 100
                doc.parse_stage = "待确认"
                revision.parsed_content = doc.parsed_content
                revision.parsed_title = doc.parsed_title
                revision.parsed_summary = doc.parsed_summary
                revision.parse_progress = 100
                revision.parse_stage = "待确认"
            except Exception as e:
                doc.status = "failed"
                doc.error_message = str(e)[:4000]
                doc.parse_stage = "解析失败"
                revision.status = "failed"
                revision.error_message = doc.error_message
                revision.parse_stage = "解析失败"
                log.exception("preview parse failed doc=%s", doc_id)

        session.add(doc)
        session.add(revision)
        created.append(doc)
        created_revision_ids[doc_id] = revision_id

    await session.commit()

    from app.workers.document_tasks import process_document_task, run_document_ingest

    out: list[DocumentOut] = []
    for d in created:
        await session.refresh(d)
        out.append(DocumentOut.model_validate(d))
        if d.status == "preview":
            needs_preview.append(d.id)
            continue
        if d.status == "failed":
            continue
        if s.ingest_background_thread:
            log.info("ingest queue (BackgroundTasks): doc=%s", d.id)
            background_tasks.add_task(run_document_ingest, d.id, created_revision_ids[d.id])
        else:
            process_document_task.delay(d.id, created_revision_ids[d.id])

    return DocumentUploadResponse(
        documents=out, skipped_duplicates=skipped, needs_preview=needs_preview
    )


# 兼容旧名
upload_pdfs = upload_documents


async def _document_revision(
    session: AsyncSession,
    doc: Document,
    *,
    prefer_pending: bool = True,
) -> DocumentRevision | None:
    revision_id = (doc.pending_revision_id if prefer_pending else None) or doc.current_revision_id
    if not revision_id:
        return None
    return await session.get(DocumentRevision, revision_id)


async def get_parsed_content(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
) -> DocumentParsedContentOut:
    doc = await get_document(session, user_id, kb_id, doc_id)
    if doc.status not in ("preview", "done", "pending", "processing"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前文档无预览内容")
    revision = await _document_revision(session, doc)
    content = revision.parsed_content if revision is not None else doc.parsed_content
    if not content and doc.status == "done":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该文档已完成自动解析，请查看解析条目")
    if not content:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "预览内容为空")
    return DocumentParsedContentOut(
        doc_id=doc.id,
        filename=revision.filename if revision is not None else doc.filename,
        file_type=revision.file_type if revision is not None else doc.file_type,
        title=(revision.parsed_title if revision is not None else doc.parsed_title) or doc.title,
        summary=revision.parsed_summary if revision is not None else doc.parsed_summary,
        content=content,
        status=revision.status if revision is not None else doc.status,
    )


async def update_parsed_content(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    body: DocumentParsedContentUpdate,
) -> DocumentParsedContentOut:
    doc = await get_document(session, user_id, kb_id, doc_id)
    if doc.status not in ("preview", "done"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "仅「待预览」或「已完成」文档可编辑")
    doc.parsed_content = clamp_mediumtext(body.content.strip()) or ""
    if body.title is not None:
        doc.parsed_title = body.title.strip()[:512] or None
        doc.title = doc.parsed_title
    if body.summary is not None:
        doc.parsed_summary = body.summary.strip()[:500] or None
    revision = await _document_revision(session, doc)
    if revision is not None:
        revision.parsed_content = doc.parsed_content
        revision.parsed_title = doc.parsed_title
        revision.parsed_summary = doc.parsed_summary
    await session.commit()
    await session.refresh(doc)
    return DocumentParsedContentOut(
        doc_id=doc.id,
        filename=revision.filename if revision is not None else doc.filename,
        file_type=revision.file_type if revision is not None else doc.file_type,
        title=doc.parsed_title or doc.title,
        summary=doc.parsed_summary,
        content=doc.parsed_content or "",
        status=revision.status if revision is not None else doc.status,
    )


async def confirm_document_import(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
) -> DocumentConfirmImportResponse:
    doc = await get_document(session, user_id, kb_id, doc_id)
    if doc.status != "preview":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "仅「待预览」状态的文档可确认入库")
    if not (doc.parsed_content or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "预览内容为空，请先编辑或重新上传")

    transition_document(
        doc,
        DocumentStatus.PENDING,
        progress=0,
        stage="排队中",
    )
    revision = await _document_revision(session, doc)
    if revision is not None:
        revision.status = "pending"
        revision.parse_progress = 0
        revision.parse_stage = "排队中"
        revision.error_message = None
    await session.commit()
    await session.refresh(doc)

    from app.workers.document_tasks import process_document_task, run_document_ingest

    settings = get_settings()
    if settings.ingest_background_thread:
        background_tasks.add_task(run_document_ingest, doc.id, revision.id if revision else None)
    else:
        process_document_task.delay(doc.id, revision.id if revision else None)

    return DocumentConfirmImportResponse(document=DocumentOut.model_validate(doc))


async def list_documents(session: AsyncSession, user_id: str, kb_id: str) -> list[Document]:
    await _ensure_kb(session, user_id, kb_id)
    q = (
        select(Document)
        .where(Document.kb_id == kb_id, Document.lifecycle_status == "active")
        .order_by(Document.created_at.desc())
    )
    r = await session.execute(q)
    return list(r.scalars().all())


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def _ensure_current_revision(
    session: AsyncSession,
    doc: Document,
) -> DocumentRevision:
    """为迁移前文档按需补建 v1，避免升级时全库重新解析。"""
    if doc.current_revision_id:
        current = await session.get(DocumentRevision, doc.current_revision_id)
        if current is not None:
            return current
    if doc.pending_revision_id:
        pending = await session.get(DocumentRevision, doc.pending_revision_id)
        if pending is not None:
            return pending

    source_hash = doc.sha256
    if not source_hash:
        try:
            source_hash = await asyncio.to_thread(_sha256_file, document_filesystem_path(doc))
        except Exception:
            source_hash = hashlib.sha256((doc.md5 or doc.storage_key).encode("utf-8")).hexdigest()
    revision = DocumentRevision(
        id=new_uuid(),
        document_id=doc.id,
        created_by=doc.user_id,
        revision_no=1,
        filename=doc.filename,
        file_type=doc.file_type,
        storage_key=doc.storage_key,
        file_bytes=doc.file_bytes,
        source_sha256=source_hash,
        pipeline_fingerprint=pipeline_fingerprint(),
        parsed_title=doc.parsed_title,
        parsed_summary=doc.parsed_summary,
        parsed_content=doc.parsed_content,
        status="active" if doc.status == "done" else doc.status,
        parse_progress=doc.parse_progress,
        parse_stage=doc.parse_stage,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count,
        activated_at=doc.updated_at if doc.status == "done" else None,
    )
    session.add(revision)
    await session.flush()
    rows = await session.execute(
        select(DocumentChunk).where(
            DocumentChunk.document_id == doc.id,
            DocumentChunk.revision_id.is_(None),
        )
    )
    for chunk in rows.scalars().all():
        chunk.revision_id = revision.id
    doc.sha256 = source_hash
    doc.current_revision_id = revision.id if doc.status == "done" else None
    if doc.status != "done":
        doc.pending_revision_id = revision.id
    return revision


def _revision_out(revision: DocumentRevision, *, current_id: str | None) -> DocumentRevisionOut:
    return DocumentRevisionOut.model_validate(revision).model_copy(
        update={"is_current": revision.id == current_id}
    )


async def _get_document_for_update(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
) -> Document:
    await _ensure_kb(session, user_id, kb_id)
    result = await session.execute(
        select(Document)
        .where(
            Document.id == doc_id,
            Document.kb_id == kb_id,
            Document.user_id == user_id,
            Document.lifecycle_status == "active",
        )
        .with_for_update()
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return doc


async def upload_document_version(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    upload: UploadFile,
    background_tasks: BackgroundTasks,
) -> DocumentVersionUploadResponse:
    """为逻辑文档创建不可变新版本；旧版本在新版本成功前继续服务。"""
    doc = await _get_document_for_update(session, user_id, kb_id, doc_id)
    if doc.status != "done" or doc.lifecycle_status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "仅可更新已完成且有效的文档")
    if doc.pending_revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文档已有版本正在处理")

    raw_name = _safe_filename(upload.filename, doc.filename)
    ext = Path(raw_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的文件格式：{ext or raw_name}")
    file_type = detect_file_type(raw_name)
    data = await upload.read()
    max_bytes = get_settings().pdf_max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过上传大小限制")
    _validate_file_magic(data, file_type, raw_name)
    source_hash = sha256_bytes(data)

    current = await _ensure_current_revision(session, doc)
    if source_hash == current.source_sha256:
        await session.commit()
        await session.refresh(doc)
        return DocumentVersionUploadResponse(
            document=DocumentOut.model_validate(doc),
            revision=_revision_out(current, current_id=doc.current_revision_id),
            unchanged=True,
        )

    max_revision = await session.scalar(
        select(func.max(DocumentRevision.revision_no)).where(DocumentRevision.document_id == doc.id)
    )
    revision_id = new_uuid()
    revision_no = int(max_revision or 0) + 1
    key = f"users/{user_id}/kb/{kb_id}/docs/{doc.id}/revisions/{revision_id}/{raw_name}"
    storage = get_blob_storage()
    await storage.put_bytes(key, data)
    revision = DocumentRevision(
        id=revision_id,
        document_id=doc.id,
        created_by=user_id,
        revision_no=revision_no,
        filename=raw_name,
        file_type=file_type.value,
        storage_key=key,
        file_bytes=len(data),
        source_sha256=source_hash,
        pipeline_fingerprint=pipeline_fingerprint(),
        status="pending",
        parse_stage="新版本排队中",
    )
    session.add(revision)
    doc.pending_revision_id = revision.id
    doc.parse_progress = 0
    doc.parse_stage = f"v{revision_no} 更新排队中"
    doc.error_message = None
    doc.lock_version = int(doc.lock_version or 0) + 1
    await session.commit()
    await session.refresh(doc)
    await session.refresh(revision)

    from app.workers.document_tasks import process_document_task, run_document_ingest

    if get_settings().ingest_background_thread:
        background_tasks.add_task(run_document_ingest, doc.id, revision.id)
    else:
        process_document_task.delay(doc.id, revision.id)
    return DocumentVersionUploadResponse(
        document=DocumentOut.model_validate(doc),
        revision=_revision_out(revision, current_id=doc.current_revision_id),
    )


async def list_document_versions(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
) -> list[DocumentRevisionOut]:
    doc = await get_document(session, user_id, kb_id, doc_id)
    await _ensure_current_revision(session, doc)
    await session.commit()
    rows = await session.execute(
        select(DocumentRevision)
        .where(DocumentRevision.document_id == doc.id)
        .order_by(DocumentRevision.revision_no.desc())
    )
    return [
        _revision_out(revision, current_id=doc.current_revision_id)
        for revision in rows.scalars().all()
    ]


async def rollback_document_version(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    revision_id: str,
    background_tasks: BackgroundTasks,
) -> DocumentVersionUploadResponse:
    doc = await _get_document_for_update(session, user_id, kb_id, doc_id)
    if doc.pending_revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文档已有版本正在处理")
    revision = await session.get(DocumentRevision, revision_id)
    if revision is None or revision.document_id != doc.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档版本不存在")
    if revision.id == doc.current_revision_id:
        return DocumentVersionUploadResponse(
            document=DocumentOut.model_validate(doc),
            revision=_revision_out(revision, current_id=doc.current_revision_id),
            unchanged=True,
        )
    if revision.status not in ("active", "superseded"):
        raise HTTPException(status.HTTP_409_CONFLICT, "该版本尚未成功处理，无法回滚")

    revision.status = "pending"
    revision.parse_progress = 0
    revision.parse_stage = "回滚重建排队中"
    revision.error_message = None
    doc.pending_revision_id = revision.id
    doc.parse_progress = 0
    doc.parse_stage = f"回滚到 v{revision.revision_no} 排队中"
    doc.error_message = None
    await session.commit()
    await session.refresh(doc)

    from app.workers.document_tasks import process_document_task, run_document_ingest

    if get_settings().ingest_background_thread:
        background_tasks.add_task(run_document_ingest, doc.id, revision.id)
    else:
        process_document_task.delay(doc.id, revision.id)
    return DocumentVersionUploadResponse(
        document=DocumentOut.model_validate(doc),
        revision=_revision_out(revision, current_id=doc.current_revision_id),
    )


async def retry_document_version(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    revision_id: str,
    background_tasks: BackgroundTasks,
) -> DocumentVersionUploadResponse:
    """重试失败版本；当前生效版本保持不变，直到本次重试成功激活。"""
    doc = await _get_document_for_update(session, user_id, kb_id, doc_id)
    if doc.pending_revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文档已有版本正在处理")
    revision = await session.get(DocumentRevision, revision_id)
    if revision is None or revision.document_id != doc.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档版本不存在")
    if revision.status != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, "仅失败版本可以重试")

    revision.status = "pending"
    revision.parse_progress = 0
    revision.parse_stage = "重试排队中"
    revision.error_message = None
    doc.pending_revision_id = revision.id
    doc.parse_progress = 0
    doc.parse_stage = f"v{revision.revision_no} 重试排队中"
    doc.error_message = None
    await session.commit()
    await session.refresh(doc)

    from app.workers.document_tasks import process_document_task, run_document_ingest

    if get_settings().ingest_background_thread:
        background_tasks.add_task(run_document_ingest, doc.id, revision.id)
    else:
        process_document_task.delay(doc.id, revision.id)
    return DocumentVersionUploadResponse(
        document=DocumentOut.model_validate(doc),
        revision=_revision_out(revision, current_id=doc.current_revision_id),
    )


async def reconcile_document_indexes(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
) -> DocumentIndexReconcileResponse:
    """以 MySQL 当前版本 chunk 清单为事实源，幂等修复两套检索索引。"""
    from app.indexing.vector_factory import get_vector_index
    from app.indexing.whoosh_index import whoosh_list_chunk_ids_for_doc

    doc = await get_document(session, user_id, kb_id, doc_id)
    if not doc.current_revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档尚无已激活版本")
    chunks_result = await session.execute(
        select(DocumentChunk)
        .where(
            DocumentChunk.document_id == doc.id,
            DocumentChunk.revision_id == doc.current_revision_id,
            DocumentChunk.lifecycle_status == "published",
        )
        .order_by(DocumentChunk.ordinal.asc())
    )
    chunks = list(chunks_result.scalars().all())
    item = (
        (
            await session.execute(
                select(KnowledgeItem).where(
                    KnowledgeItem.document_id == doc.id,
                    KnowledgeItem.lifecycle_status == "published",
                )
            )
        )
        .scalars()
        .first()
    )
    if chunks and item is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档知识条目缺失，无法自动修复索引")

    settings = get_settings()
    vector_index = get_vector_index()
    vector_list = getattr(vector_index, "list_chunk_ids_for_doc", None)
    vector_ids = (
        set(await asyncio.to_thread(vector_list, doc.id)) if callable(vector_list) else set()
    )
    keyword_ids = set(
        await asyncio.to_thread(
            whoosh_list_chunk_ids_for_doc,
            settings.whoosh_index_root,
            doc.id,
        )
    )
    expected = {chunk.chunk_id for chunk in chunks}
    orphan_ids = sorted((vector_ids | keyword_ids) - expected)
    if orphan_ids:
        await asyncio.to_thread(item_indexing.remove_index_chunks, orphan_ids)

    missing_ids = expected - (vector_ids & keyword_ids)
    repair_rows: list[dict] = []
    unrecoverable = 0
    for chunk in chunks:
        if chunk.chunk_id not in missing_ids:
            continue
        if not chunk.embedding:
            unrecoverable += 1
            continue
        repair_rows.append(
            item_indexing.build_index_row(
                chunk_id=chunk.chunk_id,
                kb_id=doc.kb_id,
                user_id=doc.user_id,
                doc_id=doc.id,
                revision_id=doc.current_revision_id,
                item_id=item.id if item is not None else "",
                page=chunk.page,
                text=chunk.text,
                vector=[float(value) for value in chunk.embedding],
                lifecycle_status="published",
            )
        )
    if repair_rows:
        await asyncio.to_thread(item_indexing.upsert_index_rows, repair_rows)

    return DocumentIndexReconcileResponse(
        expected_chunks=len(expected),
        vector_chunks=len(vector_ids),
        keyword_chunks=len(keyword_ids),
        repaired_chunks=len(repair_rows),
        removed_orphans=len(orphan_ids),
        unrecoverable_chunks=unrecoverable,
    )


async def retry_document_parse(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
) -> DocumentOut:
    """对卡在 pending / 队列丢失的 failed 文档重新投递 Celery。"""
    await _ensure_kb(session, user_id, kb_id)
    doc = await session.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id or doc.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    if doc.status not in ("pending", "failed", "processing"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "仅「待处理」「解析中」或「失败」的文档可重试解析",
        )
    transition_document(
        doc,
        DocumentStatus.PENDING,
        progress=0,
        stage="排队中",
    )
    revision = await _document_revision(session, doc)
    if revision is not None:
        revision.status = "pending"
        revision.parse_progress = 0
        revision.parse_stage = "排队中"
        revision.error_message = None
    await session.commit()
    await session.refresh(doc)

    from app.workers.document_tasks import process_document_task, run_document_ingest

    settings = get_settings()
    if settings.ingest_background_thread:
        background_tasks.add_task(run_document_ingest, doc.id, revision.id if revision else None)
    else:
        process_document_task.delay(doc.id, revision.id if revision else None)
    return DocumentOut.model_validate(doc)


async def reindex_document(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
) -> DocumentOut:
    """基于当前文件创建新版本并重跑管线；旧版本持续服务直到新版本激活。"""
    doc = await _get_document_for_update(session, user_id, kb_id, doc_id)
    if doc.status != "done":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "仅已完成入库的文档可执行增量更新")
    if doc.pending_revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文档已有版本正在处理")
    current = await _ensure_current_revision(session, doc)
    max_revision = await session.scalar(
        select(func.max(DocumentRevision.revision_no)).where(DocumentRevision.document_id == doc.id)
    )
    revision = DocumentRevision(
        id=new_uuid(),
        document_id=doc.id,
        created_by=user_id,
        revision_no=int(max_revision or 0) + 1,
        filename=current.filename,
        file_type=current.file_type,
        storage_key=current.storage_key,
        file_bytes=current.file_bytes,
        source_sha256=current.source_sha256,
        pipeline_fingerprint=pipeline_fingerprint(),
        parsed_title=current.parsed_title,
        parsed_summary=current.parsed_summary,
        parsed_content=current.parsed_content,
        status="pending",
        parse_stage="增量更新排队中",
    )
    session.add(revision)
    doc.pending_revision_id = revision.id
    doc.parse_progress = 0
    doc.parse_stage = "增量更新排队中"
    doc.error_message = None
    await session.commit()
    await session.refresh(doc)

    from app.workers.document_tasks import process_document_task, run_document_ingest

    settings = get_settings()
    if settings.ingest_background_thread:
        background_tasks.add_task(run_document_ingest, doc.id, revision.id)
    else:
        process_document_task.delay(doc.id, revision.id)
    return DocumentOut.model_validate(doc)


async def get_document(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
) -> Document:
    await _ensure_kb(session, user_id, kb_id)
    doc = await session.get(Document, doc_id)
    if (
        doc is None
        or doc.kb_id != kb_id
        or doc.user_id != user_id
        or doc.lifecycle_status != "active"
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return doc


def document_filesystem_path(doc: Document) -> str:
    storage = get_blob_storage()
    path = Path(storage.filesystem_path(doc.storage_key))
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档文件不存在")
    return str(path)


def document_media_type(doc: Document) -> str:
    ext = Path(doc.filename).suffix.lower()
    ext_mime = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    if ext in ext_mime:
        return ext_mime[ext]
    ft = FileType(doc.file_type) if doc.file_type else FileType.PDF
    from app.ingest.types import MIME_BY_TYPE

    return MIME_BY_TYPE.get(ft, "application/octet-stream")


async def delete_document(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """先逻辑删除使检索立即失效，再异步清理文件和索引。"""
    doc = await _get_document_for_update(session, user_id, kb_id, doc_id)
    revision_keys_result = await session.execute(
        select(DocumentRevision.storage_key).where(DocumentRevision.document_id == doc_id)
    )
    storage_keys = {str(key) for key in revision_keys_result.scalars().all() if key}
    if doc.storage_key:
        storage_keys.add(doc.storage_key)
    q = await session.execute(select(KnowledgeItem).where(KnowledgeItem.document_id == doc_id))
    items = list(q.scalars().all())

    for item in items:
        item.lifecycle_status = "archived"

    was_done = doc.current_revision_id is not None or doc.status == "done"
    doc.lifecycle_status = "deleted"
    doc.deleted_at = datetime.now(UTC)
    doc.status = "deleted"
    doc.pending_revision_id = None
    doc.parse_stage = "等待物理清理"

    kb = await session.get(KnowledgeBase, kb_id)
    if kb is not None and was_done:
        kb.doc_count = max(0, int(kb.doc_count or 0) - 1)

    await session.commit()

    if background_tasks is not None:
        background_tasks.add_task(_purge_document_assets, doc_id)
    else:
        await _remove_document_assets(doc_id, storage_keys)
        doc.lifecycle_status = "purged"
        doc.parse_stage = "已清理"
        revisions = await session.execute(
            select(DocumentRevision).where(DocumentRevision.document_id == doc_id)
        )
        for revision in revisions.scalars().all():
            revision.status = "purged"
        await session.commit()


async def _remove_document_assets(doc_id: str, storage_keys: set[str]) -> None:
    await asyncio.to_thread(item_indexing.remove_index_for_document, doc_id)
    storage = get_blob_storage()
    for key in storage_keys:
        try:
            await storage.delete(key)
        except Exception:
            log.warning("purge document %s: blob remove failed key=%s", doc_id, key, exc_info=True)
            raise


async def _purge_document_assets(doc_id: str) -> None:
    """幂等清理任务；失败交由 BackgroundTasks 日志/后续对账重试。"""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        doc = await session.get(Document, doc_id)
        if doc is None:
            return
        rows = await session.execute(
            select(DocumentRevision.storage_key).where(DocumentRevision.document_id == doc_id)
        )
        storage_keys = {str(key) for key in rows.scalars().all() if key}
        if doc.storage_key:
            storage_keys.add(doc.storage_key)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            await _remove_document_assets(doc_id, storage_keys)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            log.warning(
                "purge document %s failed attempt=%s",
                doc_id,
                attempt + 1,
                exc_info=True,
            )
            if attempt < 2:
                await asyncio.sleep(2**attempt)
    if last_error is not None:
        raise last_error

    async with factory() as session:
        doc = await session.get(Document, doc_id)
        if doc is not None and doc.lifecycle_status == "deleted":
            doc.lifecycle_status = "purged"
            doc.parse_stage = "已清理"
            revisions = await session.execute(
                select(DocumentRevision).where(DocumentRevision.document_id == doc_id)
            )
            for revision in revisions.scalars().all():
                revision.status = "purged"
            await session.commit()
