from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

import logging

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.schemas.knowledge_item import (
    ImportDraftsRequest,
    KnowledgeItemCreate,
    KnowledgeItemOut,
    KnowledgeItemUpdate,
    UrlImportPreviewOut,
    UrlImportRequest,
    UrlPreviewRequest,
)
from app.services import knowledge_base_service as kb_service
from app.services import knowledge_extract_service as extract_svc
from app.services import knowledge_item_service as item_service
from app.services.distill_service import DistillError
from app.services.url_import_service import distill_url_to_item_fields, fetch_url_text
from app.services.rate_limit_service import enforce_upload_limits

router = APIRouter()
log = logging.getLogger(__name__)


async def _url_import_fields(body: UrlImportRequest | UrlPreviewRequest) -> dict:
    if isinstance(body, UrlImportRequest) and body.title and body.content:
        return {
            "title": body.title.strip(),
            "content": body.content.strip(),
            "summary": (body.summary or "").strip() or None,
            "page_title": None,
        }
    raw, page_title = await fetch_url_text(body.url)
    fields = await distill_url_to_item_fields(body.url, raw, page_title)
    fields["page_title"] = page_title
    return fields


@router.post("/{kb_id}/items/preview-url", response_model=UrlImportPreviewOut)
async def preview_url_item(
    kb_id: str,
    body: UrlPreviewRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    await enforce_upload_limits(user_id)
    try:
        await kb_service.get_knowledge_base(session, user_id, kb_id)
        fields = await _url_import_fields(body)
    except ValueError as e:
        log.warning("URL 预览失败 kb=%s url=%s: %s", kb_id, body.url, e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    return UrlImportPreviewOut(
        url=body.url,
        page_title=fields.get("page_title"),
        title=fields["title"],
        summary=fields.get("summary"),
        content=fields["content"],
    )


@router.get("/{kb_id}/items", response_model=list[KnowledgeItemOut])
async def list_items(
    kb_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    lifecycle_status: str | None = Query(default=None),
    category_id: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    document_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
):
    try:
        rows = await item_service.list_items(
            session,
            user_id,
            kb_id,
            lifecycle_status=lifecycle_status,
            category_id=category_id,
            source_type=source_type,
            document_id=document_id,
            q=q,
        )
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return [KnowledgeItemOut.model_validate(r) for r in rows]


@router.post("/{kb_id}/items", response_model=KnowledgeItemOut, status_code=status.HTTP_201_CREATED)
async def create_item(
    kb_id: str,
    body: KnowledgeItemCreate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        item = await item_service.create_item(
            session,
            user_id,
            kb_id,
            title=body.title,
            content=body.content,
            category_id=body.category_id,
            summary=body.summary,
            tags=body.tags,
            access_level=body.access_level,
            source=body.source,
            publish=body.publish,
        )
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeItemOut.model_validate(item)


@router.get("/{kb_id}/items/{item_id}", response_model=KnowledgeItemOut)
async def get_item(
    kb_id: str,
    item_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        item = await item_service.get_item(session, user_id, kb_id, item_id)
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeItemOut.model_validate(item)


@router.patch("/{kb_id}/items/{item_id}", response_model=KnowledgeItemOut)
async def update_item(
    kb_id: str,
    item_id: str,
    body: KnowledgeItemUpdate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        item = await item_service.update_item(
            session,
            user_id,
            kb_id,
            item_id,
            title=body.title,
            content=body.content,
            category_id=body.category_id,
            summary=body.summary,
            tags=body.tags,
            access_level=body.access_level,
            source=body.source,
        )
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeItemOut.model_validate(item)


@router.delete("/{kb_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    kb_id: str,
    item_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        await item_service.delete_item(session, user_id, kb_id, item_id)
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{kb_id}/items/{item_id}/publish", response_model=KnowledgeItemOut)
async def publish_item(
    kb_id: str,
    item_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        item = await item_service.publish_item(session, user_id, kb_id, item_id)
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeItemOut.model_validate(item)


@router.post("/{kb_id}/items/{item_id}/reindex", response_model=KnowledgeItemOut)
async def reindex_item(
    kb_id: str,
    item_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        item = await item_service.reindex_item(session, user_id, kb_id, item_id)
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeItemOut.model_validate(item)


@router.post("/{kb_id}/items/{item_id}/archive", response_model=KnowledgeItemOut)
async def archive_item(
    kb_id: str,
    item_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        item = await item_service.archive_item(session, user_id, kb_id, item_id)
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return KnowledgeItemOut.model_validate(item)


@router.post("/{kb_id}/items/import-url", response_model=KnowledgeItemOut, status_code=status.HTTP_201_CREATED)
async def import_url_item(
    kb_id: str,
    body: UrlImportRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    await enforce_upload_limits(user_id)
    try:
        fields = await _url_import_fields(body)
        item = await item_service.create_item(
            session,
            user_id,
            kb_id,
            title=fields["title"],
            content=fields["content"],
            category_id=body.category_id,
            summary=fields.get("summary"),
            source=body.url[:512],
            source_type="url",
            publish=body.publish,
        )
    except ValueError as e:
        log.warning("URL 采集失败 kb=%s url=%s: %s", kb_id, body.url, e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except item_service.KnowledgeItemError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    return KnowledgeItemOut.model_validate(item)


@router.post("/{kb_id}/items/import-drafts")
async def import_draft_items(
    kb_id: str,
    body: ImportDraftsRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        rows = await extract_svc.import_drafts(
            session,
            user_id,
            kb_id,
            [d.model_dump() for d in body.drafts],
            publish=body.publish,
        )
    except DistillError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return {"items": rows}
