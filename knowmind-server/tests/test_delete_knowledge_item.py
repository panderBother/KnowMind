"""删除条目：关联文档时执行可审计的逻辑删除。"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.models.orm import Document, KnowledgeItem, User
from app.services import knowledge_category_service as cat_svc
from app.services import knowledge_base_service as kb_svc
from app.services import knowledge_item_service as item_svc


@pytest.fixture
async def session(tmp_path, monkeypatch):
    whoosh_dir = tmp_path / "whoosh"
    chroma_dir = tmp_path / "chroma"
    monkeypatch.setenv("WHOOSH_INDEX_ROOT", str(whoosh_dir))
    monkeypatch.setenv("CHROMA_DATA_PATH", str(chroma_dir))
    monkeypatch.setenv("EMBEDDING_MODE", "hash")
    get_settings.cache_clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_enable_fk(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        sess.add(
            User(
                id="user-del-1",
                email="del1@test.local",
                password_hash="x",
            ),
        )
        await sess.commit()
        yield sess
    await engine.dispose()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_delete_document_linked_item_soft_deletes_document(session: AsyncSession) -> None:
    user_id = "user-del-1"
    kb = await kb_svc.create_knowledge_base(session, user_id, "删除测试库")
    cat = await cat_svc.ensure_default_category(session, user_id, kb.id)

    doc = Document(
        id="doc-del-1",
        kb_id=kb.id,
        user_id=user_id,
        filename="sample.pdf",
        storage_key="users/x/kb/x/docs/x/sample.pdf",
        status="done",
        chunk_count=1,
    )
    session.add(doc)
    await session.commit()

    item = await item_svc.create_item(
        session,
        user_id,
        kb.id,
        title="文档条目",
        content="由 PDF 解析的正文",
        category_id=cat.id,
        source_type="document",
        publish=True,
    )
    item.document_id = doc.id
    await session.commit()
    await session.refresh(item)

    await item_svc.delete_item(session, user_id, kb.id, item.id)

    stored_item = await session.get(KnowledgeItem, item.id)
    stored_doc = await session.get(Document, doc.id)
    assert stored_item is not None
    assert stored_item.lifecycle_status == "archived"
    assert stored_doc is not None
    assert stored_doc.lifecycle_status == "purged"
    assert stored_doc.deleted_at is not None
    rows = await session.execute(select(KnowledgeItem).where(KnowledgeItem.document_id == doc.id))
    assert [row.id for row in rows.scalars().all()] == [item.id]


@pytest.mark.asyncio
async def test_delete_manual_item_only(session: AsyncSession) -> None:
    user_id = "user-del-1"
    kb = await kb_svc.create_knowledge_base(session, user_id, "手动删除库")
    cat = await cat_svc.ensure_default_category(session, user_id, kb.id)

    item = await item_svc.create_item(
        session,
        user_id,
        kb.id,
        title="手动",
        content="无文档关联",
        category_id=cat.id,
        publish=False,
    )

    await item_svc.delete_item(session, user_id, kb.id, item.id)
    assert await session.get(KnowledgeItem, item.id) is None
