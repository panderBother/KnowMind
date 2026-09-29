from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.schemas.knowledge_category import (
    KnowledgeCategoryCreate,
    KnowledgeCategoryOut,
    KnowledgeCategoryTreeNode,
    KnowledgeCategoryUpdate,
)
from app.services import knowledge_category_service as cat_service

router = APIRouter()


@router.get("/{kb_id}/categories", response_model=list[KnowledgeCategoryTreeNode])
async def list_categories(
    kb_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        tree = await cat_service.list_category_tree(session, user_id, kb_id)
    except cat_service.KnowledgeCategoryError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return tree


@router.post("/{kb_id}/categories", response_model=KnowledgeCategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(
    kb_id: str,
    body: KnowledgeCategoryCreate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        cat = await cat_service.create_category(
            session,
            user_id,
            kb_id,
            body.name,
            parent_id=body.parent_id,
            sort_order=body.sort_order,
        )
    except cat_service.KnowledgeCategoryError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeCategoryOut.model_validate(cat)


@router.patch("/{kb_id}/categories/{category_id}", response_model=KnowledgeCategoryOut)
async def update_category(
    kb_id: str,
    category_id: str,
    body: KnowledgeCategoryUpdate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        cat = await cat_service.update_category(
            session,
            user_id,
            kb_id,
            category_id,
            name=body.name,
            parent_id=body.parent_id,
            sort_order=body.sort_order,
        )
    except cat_service.KnowledgeCategoryError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeCategoryOut.model_validate(cat)


@router.delete("/{kb_id}/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    kb_id: str,
    category_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        await cat_service.delete_category(session, user_id, kb_id, category_id)
    except cat_service.KnowledgeCategoryError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
