from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import Document, KnowledgeBase, KnowledgeCategory, KnowledgeItem, new_uuid
from app.services import item_indexing
from app.services.knowledge_category_service import ensure_default_category
from app.utils.db_text import clamp_mediumtext


class KnowledgeItemError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def _ensure_kb(session: AsyncSession, user_id: str, kb_id: str) -> KnowledgeBase:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.user_id != user_id:
        raise KnowledgeItemError("知识库不存在", 404)
    return kb


async def _ensure_category(session: AsyncSession, kb_id: str, category_id: str) -> KnowledgeCategory:
    cat = await session.get(KnowledgeCategory, category_id)
    if cat is None or cat.kb_id != kb_id:
        raise KnowledgeItemError("分类不存在", 404)
    return cat


async def list_items(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    *,
    lifecycle_status: str | None = None,
    category_id: str | None = None,
    source_type: str | None = None,
    document_id: str | None = None,
    q: str | None = None,
) -> list[KnowledgeItem]:
    await _ensure_kb(session, user_id, kb_id)
    stmt = select(KnowledgeItem).where(KnowledgeItem.kb_id == kb_id)
    if lifecycle_status:
        stmt = stmt.where(KnowledgeItem.lifecycle_status == lifecycle_status)
    if category_id:
        stmt = stmt.where(KnowledgeItem.category_id == category_id)
    if source_type:
        stmt = stmt.where(KnowledgeItem.source_type == source_type)
    if document_id:
        stmt = stmt.where(KnowledgeItem.document_id == document_id)
    if q and q.strip():
        kw = f"%{q.strip()}%"
        stmt = stmt.where(or_(KnowledgeItem.title.like(kw), KnowledgeItem.content.like(kw)))
    stmt = stmt.order_by(KnowledgeItem.updated_at.desc())
    r = await session.execute(stmt)
    return list(r.scalars().all())


async def get_item(session: AsyncSession, user_id: str, kb_id: str, item_id: str) -> KnowledgeItem:
    await _ensure_kb(session, user_id, kb_id)
    item = await session.get(KnowledgeItem, item_id)
    if item is None or item.kb_id != kb_id:
        raise KnowledgeItemError("知识条目不存在", 404)
    return item


async def create_item(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    *,
    title: str,
    content: str,
    category_id: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    access_level: str = "internal",
    source: str | None = None,
    source_type: str = "manual",
    publish: bool = False,
) -> KnowledgeItem:
    await _ensure_kb(session, user_id, kb_id)
    await _ensure_category(session, kb_id, category_id)
    title = title.strip()
    content = content.strip()
    if not title or not content:
        raise KnowledgeItemError("标题与正文不能为空", 400)

    now = datetime.now(UTC)
    status = "published" if publish else "draft"
    chunk_id = new_uuid() if publish else None
    item = KnowledgeItem(
        id=new_uuid(),
        kb_id=kb_id,
        user_id=user_id,
        category_id=category_id,
        source_type=source_type,
        title=title[:200],
        content=content,
        summary=(summary or "")[:500] or None,
        tags=tags or [],
        lifecycle_status=status,
        access_level=access_level,
        source=source,
        chunk_id=chunk_id,
        published_at=now if publish else None,
    )
    session.add(item)
    await session.flush()
    await session.refresh(item)

    if publish and chunk_id:
        try:
            first_id = await asyncio.to_thread(
                item_indexing.index_text_item,
                chunk_id=chunk_id,
                kb_id=kb_id,
                user_id=user_id,
                item_id=item.id,
                text=content,
                title=title,
                lifecycle_status="published",
            )
        except Exception as e:
            await session.rollback()
            raise KnowledgeItemError(f"发布失败：检索索引写入失败（{e!s}）", 502) from e
        if first_id:
            item.chunk_id = first_id
    await session.commit()
    await session.refresh(item)
    return item


async def update_item(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    item_id: str,
    *,
    title: str | None = None,
    content: str | None = None,
    category_id: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    access_level: str | None = None,
    source: str | None = None,
) -> KnowledgeItem:
    item = await get_item(session, user_id, kb_id, item_id)
    if item.source_type == "url" and any(v is not None for v in (title, content, summary)):
        raise KnowledgeItemError("URL 采集条目为只读，不可修改正文", 400)
    if category_id is not None:
        await _ensure_category(session, kb_id, category_id)
        item.category_id = category_id
    if title is not None:
        t = title.strip()
        if not t:
            raise KnowledgeItemError("标题不能为空", 400)
        item.title = t[:200]
    if content is not None:
        c = content.strip()
        if not c:
            raise KnowledgeItemError("正文不能为空", 400)
        item.content = c
    if summary is not None:
        item.summary = summary[:500] if summary else None
    if tags is not None:
        item.tags = tags
    if access_level is not None:
        item.access_level = access_level
    if source is not None:
        item.source = source

    if content is not None and item.document_id:
        doc = await session.get(Document, item.document_id)
        if doc is not None:
            doc.parsed_content = clamp_mediumtext(item.content)
            if title is not None:
                doc.parsed_title = item.title[:512]

    await session.commit()
    await session.refresh(item)

    if item.lifecycle_status == "published":
        try:
            if item.source_type == "document" and item.document_id:
                first_id = await asyncio.to_thread(
                    item_indexing.reindex_document_item,
                    kb_id=kb_id,
                    user_id=user_id,
                    doc_id=item.document_id,
                    item_id=item.id,
                    text=item.content,
                    title=item.title,
                    lifecycle_status="published",
                )
            elif item.chunk_id:
                first_id = await asyncio.to_thread(
                    item_indexing.index_text_item,
                    chunk_id=item.chunk_id,
                    kb_id=kb_id,
                    user_id=user_id,
                    item_id=item.id,
                    text=item.content,
                    title=item.title,
                    lifecycle_status="published",
                    doc_id=item.document_id,
                    page=item.page or 0,
                )
            else:
                first_id = None
        except Exception as e:
            raise KnowledgeItemError(f"更新索引失败：{e!s}", 502) from e
        if first_id and first_id != item.chunk_id:
            item.chunk_id = first_id
            await session.commit()
            await session.refresh(item)
    return item


async def publish_item(session: AsyncSession, user_id: str, kb_id: str, item_id: str) -> KnowledgeItem:
    item = await get_item(session, user_id, kb_id, item_id)
    if not item.category_id:
        default_cat = await ensure_default_category(session, user_id, kb_id)
        item.category_id = default_cat.id
    if not item.chunk_id:
        item.chunk_id = new_uuid()
    item.lifecycle_status = "published"
    item.published_at = datetime.now(UTC)
    await session.flush()
    await _write_search_index(session, item, kb_id=kb_id, user_id=user_id)
    await session.commit()
    await session.refresh(item)
    return item


async def reindex_item(session: AsyncSession, user_id: str, kb_id: str, item_id: str) -> KnowledgeItem:
    """已发布条目重建向量/全文索引（修复「已发布但检索不到」）。"""
    item = await get_item(session, user_id, kb_id, item_id)
    if item.lifecycle_status != "published":
        raise KnowledgeItemError("仅已发布条目可重建索引", 400)
    if not item.chunk_id:
        item.chunk_id = new_uuid()
    await session.flush()
    await _write_search_index(session, item, kb_id=kb_id, user_id=user_id)
    await session.commit()
    await session.refresh(item)
    return item


async def _write_search_index(
    session: AsyncSession,
    item: KnowledgeItem,
    *,
    kb_id: str,
    user_id: str,
) -> None:
    try:
        if item.source_type == "document" and item.document_id:
            first_id = await asyncio.to_thread(
                item_indexing.reindex_document_item,
                kb_id=kb_id,
                user_id=user_id,
                doc_id=item.document_id,
                item_id=item.id,
                text=item.content,
                title=item.title,
                lifecycle_status="published",
            )
        else:
            chunk_id = item.chunk_id or new_uuid()
            first_id = await asyncio.to_thread(
                item_indexing.index_text_item,
                chunk_id=chunk_id,
                kb_id=kb_id,
                user_id=user_id,
                item_id=item.id,
                text=item.content,
                title=item.title,
                lifecycle_status="published",
                doc_id=item.document_id,
                page=item.page or 0,
            )
    except Exception as e:
        await session.rollback()
        raise KnowledgeItemError(f"检索索引写入失败（{e!s}）", 502) from e
    if first_id:
        item.chunk_id = first_id


async def archive_item(session: AsyncSession, user_id: str, kb_id: str, item_id: str) -> KnowledgeItem:
    item = await get_item(session, user_id, kb_id, item_id)
    item.lifecycle_status = "archived"
    await session.commit()
    await session.refresh(item)
    if item.document_id:
        await asyncio.to_thread(item_indexing.remove_index_for_document, item.document_id)
    elif item.chunk_id:
        await asyncio.to_thread(item_indexing.remove_index_chunk, item.chunk_id)
    await session.refresh(item)
    return item


async def delete_item(session: AsyncSession, user_id: str, kb_id: str, item_id: str) -> None:
    item = await get_item(session, user_id, kb_id, item_id)
    if item.document_id:
        from app.services.document_service import delete_document

        await delete_document(session, user_id, kb_id, item.document_id)
        return

    chunk_id = item.chunk_id
    item_pk = item.id
    await session.delete(item)
    await session.commit()
    await asyncio.to_thread(item_indexing.remove_index_for_item, item_pk, chunk_id=chunk_id)
