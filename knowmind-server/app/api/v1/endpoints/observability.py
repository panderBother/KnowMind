from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.schemas.observability import UsageSummaryOut
from app.services.observability_service import get_usage_summary

router = APIRouter()


@router.get("/usage", response_model=UsageSummaryOut)
async def usage_summary(
    days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return UsageSummaryOut.model_validate(
        await get_usage_summary(session, user_id, days=days)
    )
