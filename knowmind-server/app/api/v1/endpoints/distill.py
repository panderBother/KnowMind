from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.schemas.knowledge_item import KnowledgeGapOut
from app.services import distill_service as distill_svc

router = APIRouter()


@router.get("/{kb_id}/distill/gaps", response_model=list[KnowledgeGapOut])
async def list_distill_gaps(
    kb_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    rows = await distill_svc.list_gaps(session, user_id, kb_id)
    return [KnowledgeGapOut.model_validate(r) for r in rows]


@router.post("/{kb_id}/distill/analyze", response_model=list[KnowledgeGapOut])
async def analyze_distill_gaps(
    kb_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    rows = await distill_svc.analyze_gaps(session, user_id, kb_id)
    return [KnowledgeGapOut.model_validate(r) for r in rows]


@router.post("/{kb_id}/distill/gaps/{gap_id}/generate")
async def generate_gap_drafts(
    kb_id: str,
    gap_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        drafts = await distill_svc.generate_drafts_for_gap(session, user_id, kb_id, gap_id)
    except distill_svc.DistillError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return {"drafts": drafts}
