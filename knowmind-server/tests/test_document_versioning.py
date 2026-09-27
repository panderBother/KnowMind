from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, UploadFile
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.ingest.fingerprints import sha256_bytes
from app.models.orm import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeItem,
    User,
)
from app.services import document_service


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes) -> None:
        self.objects[key] = data


@pytest.fixture
async def version_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_enable_fk(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(User(id="version-user", email="version@test.local", password_hash="x"))
        session.add(KnowledgeBase(id="version-kb", user_id="version-user", name="版本库"))
        revision = DocumentRevision(
            id="revision-v1",
            document_id="version-doc",
            created_by="version-user",
            revision_no=1,
            filename="guide.md",
            file_type="markdown",
            storage_key="docs/version-doc/revision-v1/guide.md",
            file_bytes=8,
            source_sha256=sha256_bytes(b"version1"),
            pipeline_fingerprint="pipeline-v1",
            status="active",
            chunk_count=1,
        )
        document = Document(
            id="version-doc",
            kb_id="version-kb",
            user_id="version-user",
            filename="guide.md",
            file_type="markdown",
            storage_key=revision.storage_key,
            status="done",
            file_bytes=8,
            sha256=revision.source_sha256,
            current_revision_id=revision.id,
            chunk_count=1,
        )
        session.add(document)
        session.add(revision)
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_new_version_keeps_current_revision_until_worker_activates(
    version_session: AsyncSession,
) -> None:
    storage = MemoryStorage()
    upload = UploadFile(filename="guide.md", file=BytesIO(b"version2 changed"))
    with (
        patch("app.services.document_service.get_blob_storage", return_value=storage),
        patch("app.workers.document_tasks.process_document_task.delay") as queue,
    ):
        response = await document_service.upload_document_version(
            version_session,
            "version-user",
            "version-kb",
            "version-doc",
            upload,
            BackgroundTasks(),
        )

    assert response.unchanged is False
    assert response.revision is not None
    assert response.revision.revision_no == 2
    assert response.document.current_revision_id == "revision-v1"
    assert response.document.pending_revision_id == response.revision.id
    assert response.document.status == "done"
    assert list(storage.objects.values()) == [b"version2 changed"]
    queue.assert_called_once_with("version-doc", response.revision.id)


@pytest.mark.asyncio
async def test_identical_version_is_noop(version_session: AsyncSession) -> None:
    storage = MemoryStorage()
    upload = UploadFile(filename="guide.md", file=BytesIO(b"version1"))
    with patch("app.services.document_service.get_blob_storage", return_value=storage):
        response = await document_service.upload_document_version(
            version_session,
            "version-user",
            "version-kb",
            "version-doc",
            upload,
            BackgroundTasks(),
        )

    assert response.unchanged is True
    assert response.revision is not None
    assert response.revision.id == "revision-v1"
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_failed_version_can_be_retried_without_replacing_current(
    version_session: AsyncSession,
) -> None:
    failed = DocumentRevision(
        id="revision-v2-failed",
        document_id="version-doc",
        created_by="version-user",
        revision_no=2,
        filename="guide.md",
        file_type="markdown",
        storage_key="docs/version-doc/revision-v2/guide.md",
        file_bytes=9,
        source_sha256=sha256_bytes(b"failed-v2"),
        pipeline_fingerprint="pipeline-v1",
        status="failed",
        error_message="temporary error",
    )
    version_session.add(failed)
    await version_session.commit()

    with patch("app.workers.document_tasks.process_document_task.delay") as queue:
        response = await document_service.retry_document_version(
            version_session,
            "version-user",
            "version-kb",
            "version-doc",
            failed.id,
            BackgroundTasks(),
        )

    assert response.document.current_revision_id == "revision-v1"
    assert response.document.pending_revision_id == failed.id
    assert response.revision is not None
    assert response.revision.status == "pending"
    queue.assert_called_once_with("version-doc", failed.id)


@pytest.mark.asyncio
async def test_reconcile_repairs_missing_index_and_removes_orphan(
    version_session: AsyncSession,
) -> None:
    version_session.add(
        KnowledgeItem(
            id="version-item",
            kb_id="version-kb",
            user_id="version-user",
            document_id="version-doc",
            source_type="document",
            title="Guide",
            content="current text",
            lifecycle_status="published",
        )
    )
    version_session.add(
        DocumentChunk(
            id="chunk-row",
            document_id="version-doc",
            revision_id="revision-v1",
            chunk_id="current-chunk",
            content_hash="content-hash",
            embedding=[0.1, 0.2],
            ordinal=0,
            page=1,
            text="current text",
        )
    )
    await version_session.commit()

    class FakeVectorIndex:
        def list_chunk_ids_for_doc(self, doc_id: str) -> list[str]:
            assert doc_id == "version-doc"
            return ["orphan-chunk"]

    repaired: list[dict] = []
    removed: list[str] = []
    with (
        patch("app.indexing.vector_factory.get_vector_index", return_value=FakeVectorIndex()),
        patch(
            "app.indexing.whoosh_index.whoosh_list_chunk_ids_for_doc",
            return_value=["current-chunk"],
        ),
        patch(
            "app.services.document_service.item_indexing.upsert_index_rows",
            side_effect=lambda rows: repaired.extend(rows),
        ),
        patch(
            "app.services.document_service.item_indexing.remove_index_chunks",
            side_effect=lambda ids: removed.extend(ids),
        ),
    ):
        result = await document_service.reconcile_document_indexes(
            version_session,
            "version-user",
            "version-kb",
            "version-doc",
        )

    assert result.expected_chunks == 1
    assert result.repaired_chunks == 1
    assert result.removed_orphans == 1
    assert removed == ["orphan-chunk"]
    assert repaired[0]["chunk_id"] == "current-chunk"
    assert repaired[0]["revision_id"] == "revision-v1"
