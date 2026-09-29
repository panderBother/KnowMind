from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.orm import KnowledgeBase, KnowledgeCategory, new_uuid

DEFAULT_CATEGORY_NAME = "未分类"


class KnowledgeCategoryError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def _ensure_kb(session: AsyncSession, user_id: str, kb_id: str) -> KnowledgeBase:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.user_id != user_id:
        raise KnowledgeCategoryError("知识库不存在", 404)
    return kb


def get_or_create_default_category_sync(session: Session, kb_id: str, user_id: str) -> str:
    q = select(KnowledgeCategory).where(
        KnowledgeCategory.kb_id == kb_id,
        KnowledgeCategory.name == DEFAULT_CATEGORY_NAME,
    )
    row = session.execute(q).scalar_one_or_none()
    if row is not None:
        return row.id
    cat = KnowledgeCategory(
        id=new_uuid(),
        kb_id=kb_id,
        user_id=user_id,
        name=DEFAULT_CATEGORY_NAME,
        sort_order=0,
    )
    session.add(cat)
    session.flush()
    return cat.id


async def ensure_default_category(session: AsyncSession, user_id: str, kb_id: str) -> KnowledgeCategory:
    await _ensure_kb(session, user_id, kb_id)
    q = select(KnowledgeCategory).where(
        KnowledgeCategory.kb_id == kb_id,
        KnowledgeCategory.name == DEFAULT_CATEGORY_NAME,
    )
    r = await session.execute(q)
    row = r.scalar_one_or_none()
    if row is not None:
        return row
    cat = KnowledgeCategory(id=new_uuid(), kb_id=kb_id, user_id=user_id, name=DEFAULT_CATEGORY_NAME)
    session.add(cat)
    await session.flush()
    return cat


def _build_tree(rows: list[KnowledgeCategory]) -> list[dict]:
    children_map: dict[str | None, list[KnowledgeCategory]] = {}
    for c in rows:
        children_map.setdefault(c.parent_id, []).append(c)
    for lst in children_map.values():
        lst.sort(key=lambda x: (x.sort_order, x.name))

    def node(cat: KnowledgeCategory) -> dict:
        kids = children_map.get(cat.id, [])
        return {
            "id": cat.id,
            "kb_id": cat.kb_id,
            "parent_id": cat.parent_id,
            "name": cat.name,
            "sort_order": cat.sort_order,
            "created_at": cat.created_at,
            "updated_at": cat.updated_at,
            "children": [node(k) for k in kids],
        }

    roots = children_map.get(None, [])
    return [node(c) for c in roots]


async def list_category_tree(session: AsyncSession, user_id: str, kb_id: str) -> list[dict]:
    """列出分类树；若自动创建「未分类」则提交事务（避免返回未落库的 id）。"""
    await ensure_default_category(session, user_id, kb_id)
    q = select(KnowledgeCategory).where(KnowledgeCategory.kb_id == kb_id).order_by(
        KnowledgeCategory.sort_order,
        KnowledgeCategory.name,
    )
    r = await session.execute(q)
    rows = list(r.scalars().all())
    if not rows or all(c.name != DEFAULT_CATEGORY_NAME for c in rows):
        await ensure_default_category(session, user_id, kb_id)
        r = await session.execute(q)
        rows = list(r.scalars().all())
    await session.commit()
    return _build_tree(rows)


async def create_category(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    name: str,
    *,
    parent_id: str | None = None,
    sort_order: int = 0,
) -> KnowledgeCategory:
    await _ensure_kb(session, user_id, kb_id)
    name = name.strip()
    if not name:
        raise KnowledgeCategoryError("分类名称不能为空", 400)
    if parent_id:
        parent = await session.get(KnowledgeCategory, parent_id)
        if parent is None or parent.kb_id != kb_id:
            raise KnowledgeCategoryError("父分类不存在", 404)
    cat = KnowledgeCategory(
        id=new_uuid(),
        kb_id=kb_id,
        user_id=user_id,
        parent_id=parent_id,
        name=name,
        sort_order=sort_order,
    )
    session.add(cat)
    await session.commit()
    await session.refresh(cat)
    return cat


async def update_category(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    category_id: str,
    *,
    name: str | None = None,
    parent_id: str | None = None,
    sort_order: int | None = None,
) -> KnowledgeCategory:
    await _ensure_kb(session, user_id, kb_id)
    cat = await session.get(KnowledgeCategory, category_id)
    if cat is None or cat.kb_id != kb_id:
        raise KnowledgeCategoryError("分类不存在", 404)
    if name is not None:
        n = name.strip()
        if not n:
            raise KnowledgeCategoryError("分类名称不能为空", 400)
        cat.name = n
    if parent_id is not None:
        if parent_id == category_id:
            raise KnowledgeCategoryError("不能将分类设为自己的父节点", 400)
        if parent_id:
            parent = await session.get(KnowledgeCategory, parent_id)
            if parent is None or parent.kb_id != kb_id:
                raise KnowledgeCategoryError("父分类不存在", 404)
        cat.parent_id = parent_id or None
    if sort_order is not None:
        cat.sort_order = sort_order
    await session.commit()
    await session.refresh(cat)
    return cat


async def delete_category(session: AsyncSession, user_id: str, kb_id: str, category_id: str) -> None:
    await _ensure_kb(session, user_id, kb_id)
    cat = await session.get(KnowledgeCategory, category_id)
    if cat is None or cat.kb_id != kb_id:
        raise KnowledgeCategoryError("分类不存在", 404)
    if cat.name == DEFAULT_CATEGORY_NAME:
        raise KnowledgeCategoryError("默认分类「未分类」不可删除", 400)
    from app.models.orm import KnowledgeItem

    child_q = select(KnowledgeCategory.id).where(KnowledgeCategory.parent_id == category_id).limit(1)
    if (await session.execute(child_q)).scalar_one_or_none() is not None:
        raise KnowledgeCategoryError("请先删除子分类", 400)
    item_q = select(KnowledgeItem.id).where(KnowledgeItem.category_id == category_id).limit(1)
    if (await session.execute(item_q)).scalar_one_or_none() is not None:
        raise KnowledgeCategoryError("分类下仍有知识条目，无法删除", 400)
    await session.delete(cat)
    await session.commit()
