from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.models.orm import ChatMessage, Conversation
from app.schemas.conversation import (
    ChatMessageOut,
    ConversationBranchRequest,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.knowledge_item import ExtractKnowledgeRequest
from app.schemas.report import GenerateReportRequest, ResearchReportOut
from app.services import knowledge_extract_service as extract_svc
from app.services import report_service as report_svc
from app.services.distill_service import DistillError
from app.services.report_service import ReportError
from app.services.conversation_service import (
    branch_conversation,
    create_conversation,
    get_conversation_for_user,
    load_messages_ordered,
)

router = APIRouter()


@router.post("", response_model=ConversationOut)
async def create_conversation_endpoint(
    body: ConversationCreate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    conv = await create_conversation(
        session,
        user_id=user_id,
        knowledge_base_id=body.knowledge_base_id,
        deep_research=body.deep_research,
        web_search=body.web_search,
        title=body.title,
        model_mode=body.model_mode,
    )
    await session.commit()
    await session.refresh(conv)
    return conv


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    limit: int = 50,
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=100),
    expert_id: str | None = Query(default=None, description="仅列出该专家下的会话"),
    main_chat_only: bool = Query(
        default=False,
        description="为 true 时仅返回智能对话（expert_id 为空）",
    ),
):
    lim = max(1, min(limit, 100))
    stmt = select(Conversation).where(Conversation.user_id == user_id)
    if expert_id and str(expert_id).strip():
        stmt = stmt.where(Conversation.expert_id == expert_id.strip())
    elif main_chat_only:
        stmt = stmt.where(Conversation.expert_id.is_(None))
    search = (q or "").strip()
    if search:
        pattern = f"%{search}%"
        matching_conversations = select(ChatMessage.conversation_id).where(
            ChatMessage.content.ilike(pattern)
        )
        stmt = stmt.where(
            or_(Conversation.title.ilike(pattern), Conversation.id.in_(matching_conversations))
        )
    stmt = (
        stmt.order_by(
            Conversation.is_pinned.desc(),
            Conversation.updated_at.desc(),
            Conversation.id.desc(),
        )
        .offset(offset)
        .limit(lim)
    )
    q = await session.execute(stmt)
    return list(q.scalars().all())


@router.get("/{conversation_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(
    conversation_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    await get_conversation_for_user(session, conversation_id=conversation_id, user_id=user_id)
    rows = await load_messages_ordered(session, conversation_id)
    # 进程退出或连接中断可能来不及处理 CancelledError。超过安全窗口后，
    # 将遗留的 generating 标记为 stopped，让刷新后的客户端可以明确重试。
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=30)
    changed = False
    for row in rows:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if row.generation_status == "generating" and created_at < stale_before:
            row.generation_status = "stopped"
            if not row.content.strip():
                row.content = "生成中断，请重试"
            changed = True
    if changed:
        await session.commit()
    return rows


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation_endpoint(
    conversation_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """刷新前端恢复会话时拉取元数据（含 knowledge_base_id）。"""
    conv = await get_conversation_for_user(session, conversation_id=conversation_id, user_id=user_id)
    return conv


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    conv = await get_conversation_for_user(session, conversation_id=conversation_id, user_id=user_id)
    if body.title is not None:
        stripped = body.title.strip()
        conv.title = stripped if stripped else None
    if body.is_pinned is not None:
        conv.is_pinned = body.is_pinned
    await session.commit()
    await session.refresh(conv)
    return conv


@router.post("/{conversation_id}/branch", response_model=ConversationOut)
async def branch_conversation_endpoint(
    conversation_id: str,
    body: ConversationBranchRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await branch_conversation(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        from_message_id=body.from_message_id,
        title=body.title,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    conv = await get_conversation_for_user(session, conversation_id=conversation_id, user_id=user_id)
    await session.delete(conv)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{conversation_id}/extract-knowledge")
async def extract_knowledge(
    conversation_id: str,
    body: ExtractKnowledgeRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        drafts = await extract_svc.extract_knowledge_drafts(
            session,
            user_id,
            conversation_id,
            kb_id=body.kb_id,
            message_limit=body.message_limit,
        )
    except DistillError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return {"drafts": drafts}


@router.post("/{conversation_id}/generate-report", response_model=ResearchReportOut)
async def generate_report(
    conversation_id: str,
    body: GenerateReportRequest,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        row = await report_svc.generate_report_from_conversation(
            session,
            user_id,
            conversation_id,
            kb_id=body.kb_id,
            title_override=body.title,
        )
    except ReportError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    return report_svc.report_to_schema(row)
