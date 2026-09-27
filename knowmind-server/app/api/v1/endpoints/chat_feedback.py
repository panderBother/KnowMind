from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.schemas.knowledge_item import ChatFeedbackRequest
from app.services import distill_service as distill_svc

router = APIRouter()


@router.post("/feedback", status_code=204)
async def submit_chat_feedback(
    body: ChatFeedbackRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        await distill_svc.record_feedback(
            session,
            user_id=user_id,
            kb_id=body.knowledge_base_id,
            conversation_id=body.conversation_id,
            message_id=body.message_id,
            query_text=body.query_text,
            correction=body.correction,
        )
    except distill_svc.DistillError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
