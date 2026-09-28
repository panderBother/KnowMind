from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.memory_constants import approx_token_count
from app.models.orm import (
    ChatMessage,
    Conversation,
    ConversationFact,
    ConversationSummary,
    KnowledgeBase,
)


async def get_conversation_for_user(
    session: AsyncSession,
    *,
    conversation_id: str,
    user_id: str,
) -> Conversation:
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问")
    return conv


async def create_conversation(
    session: AsyncSession,
    *,
    user_id: str,
    knowledge_base_id: str | None,
    deep_research: bool,
    web_search: bool,
    title: str | None = None,
    expert_id: str | None = None,
    model_mode: str = "balanced",
    parent_conversation_id: str | None = None,
    branched_from_message_id: str | None = None,
) -> Conversation:
    kb_ok: str | None = None
    if knowledge_base_id and str(knowledge_base_id).strip():
        kb = await session.get(KnowledgeBase, knowledge_base_id.strip())
        if kb is None or kb.user_id != user_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "知识库不存在或无权使用")
        kb_ok = kb.id
    expert_ok: str | None = None
    if expert_id and str(expert_id).strip():
        from app.models.orm import ExpertAgent

        expert = await session.get(ExpertAgent, expert_id.strip())
        if expert is None or expert.user_id != user_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "专家不存在或无权使用")
        expert_ok = expert.id
        if kb_ok and expert.kb_id != kb_ok:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "专家与知识库不匹配")
        if not kb_ok:
            kb_ok = expert.kb_id
    conv = Conversation(
        user_id=user_id,
        knowledge_base_id=kb_ok,
        expert_id=expert_ok,
        deep_research=deep_research,
        web_search=web_search,
        title=title,
        model_mode=model_mode,
        parent_conversation_id=parent_conversation_id,
        branched_from_message_id=branched_from_message_id,
    )
    session.add(conv)
    await session.flush()
    return conv


async def resolve_conversation(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: str | None,
    knowledge_base_id: str | None,
    deep_research: bool,
    web_search: bool,
    expert_id: str | None = None,
    model_mode: str = "balanced",
) -> tuple[Conversation, bool]:
    """
    返回 (会话, 是否本次新建)。
    conversation_id 为空则新建会话。
    """
    expert_ok = (expert_id or "").strip() or None
    if conversation_id and str(conversation_id).strip():
        conv = await get_conversation_for_user(
            session, conversation_id=conversation_id.strip(), user_id=user_id
        )
        if expert_ok and conv.expert_id and conv.expert_id != expert_ok:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "会话不属于该专家")
        kb_ok: str | None = None
        if knowledge_base_id and str(knowledge_base_id).strip():
            kb = await session.get(KnowledgeBase, knowledge_base_id.strip())
            if kb is None or kb.user_id != user_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "知识库不存在或无权使用")
            kb_ok = kb.id
        conv.knowledge_base_id = kb_ok
        conv.deep_research = deep_research
        conv.web_search = web_search
        conv.model_mode = model_mode
        if expert_ok and not conv.expert_id:
            from app.models.orm import ExpertAgent

            expert = await session.get(ExpertAgent, expert_ok)
            if expert is None or expert.user_id != user_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "专家不存在或无权使用")
            conv.expert_id = expert.id
        return conv, False

    conv = await create_conversation(
        session,
        user_id=user_id,
        knowledge_base_id=knowledge_base_id,
        deep_research=deep_research,
        web_search=web_search,
        expert_id=expert_ok,
        model_mode=model_mode,
    )
    return conv, True


async def append_message(
    session: AsyncSession,
    *,
    conversation_id: str,
    role: str,
    content: str,
    trace_id: str | None = None,
    generation_status: str = "completed",
    reply_to_message_id: str | None = None,
    citations: list[dict] | None = None,
    attachments: list[dict] | None = None,
    tool_traces: list[dict] | None = None,
) -> ChatMessage:
    tok = approx_token_count(content)
    sequence_result = await session.execute(
        select(func.max(ChatMessage.sequence_no)).where(
            ChatMessage.conversation_id == conversation_id
        )
    )
    sequence_no = int(sequence_result.scalar_one_or_none() or 0) + 1
    m = ChatMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        trace_id=trace_id,
        sequence_no=sequence_no,
        generation_status=generation_status,
        reply_to_message_id=reply_to_message_id,
        token_est=tok,
        citations_json=citations or None,
        attachments_json=attachments or None,
        tool_traces_json=tool_traces or None,
    )
    session.add(m)
    await session.flush()
    return m


async def load_messages_ordered(session: AsyncSession, conversation_id: str) -> list[ChatMessage]:
    q = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(
            ChatMessage.sequence_no.asc(),
            ChatMessage.created_at.asc(),
            ChatMessage.id.asc(),
        ),
    )
    return list(q.scalars().all())


async def branch_conversation(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    from_message_id: str,
    title: str | None = None,
) -> Conversation:
    """复制目标提问之前的上下文，创建一条可独立继续的会话分支。"""
    source = await get_conversation_for_user(
        session, conversation_id=conversation_id, user_id=user_id
    )
    messages = await load_messages_ordered(session, source.id)
    target_index = next((i for i, row in enumerate(messages) if row.id == from_message_id), -1)
    if target_index < 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分支起点消息不存在")
    target = messages[target_index]
    source_user_id = target.reply_to_message_id if target.role == "assistant" else target.id
    source_user_index = next(
        (i for i, row in enumerate(messages) if row.id == source_user_id and row.role == "user"),
        -1,
    )
    if source_user_index < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法定位该回答对应的用户问题")

    branch_title = (title or "").strip() or f"{(source.title or '新对话')[:220]} · 分支"
    branch = await create_conversation(
        session,
        user_id=user_id,
        knowledge_base_id=source.knowledge_base_id,
        deep_research=source.deep_research,
        web_search=source.web_search,
        title=branch_title,
        expert_id=source.expert_id,
        model_mode=source.model_mode,
        parent_conversation_id=source.id,
        branched_from_message_id=target.id,
    )

    id_map: dict[str, str] = {}
    clone_rows = messages[:source_user_index]
    for row in clone_rows:
        cloned = await append_message(
            session,
            conversation_id=branch.id,
            role=row.role,
            content=row.content,
            trace_id=row.trace_id,
            generation_status=(
                row.generation_status if row.generation_status == "completed" else "stopped"
            ),
            reply_to_message_id=id_map.get(row.reply_to_message_id or ""),
            citations=row.citations_json,
            attachments=row.attachments_json,
            tool_traces=row.tool_traces_json,
        )
        id_map[row.id] = cloned.id
    await session.commit()
    await session.refresh(branch)
    return branch


async def load_summaries_concat(session: AsyncSession, conversation_id: str) -> str:
    q = await session.execute(
        select(ConversationSummary)
        .where(ConversationSummary.conversation_id == conversation_id)
        .order_by(ConversationSummary.created_at.asc()),
    )
    rows = list(q.scalars().all())
    if not rows:
        return ""
    parts: list[str] = []
    for i, s in enumerate(rows, 1):
        parts.append(f"### 摘要片段 {i}\n{s.summary_text.strip()}")
    return "\n\n".join(parts)


async def load_working_memory_concat(session: AsyncSession, conversation_id: str) -> str:
    rows = await session.execute(
        select(ConversationFact)
        .where(ConversationFact.conversation_id == conversation_id)
        .order_by(ConversationFact.updated_at.desc())
    )
    facts = list(rows.scalars().all())
    if not facts:
        return ""
    return "\n".join(f"- {fact.fact_key}: {fact.fact_value}" for fact in facts[:40])


async def count_summaries(session: AsyncSession, conversation_id: str) -> int:
    q = await session.execute(
        select(func.count())
        .select_from(ConversationSummary)
        .where(
            ConversationSummary.conversation_id == conversation_id,
        ),
    )
    return int(q.scalar() or 0)


def rows_for_redis(messages: list[ChatMessage]) -> list[dict]:
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else "",
        }
        for m in messages
    ]


def should_run_summary(conv: Conversation, *, prior_summary_count: int) -> bool:
    s = get_settings()
    vol = conv.acc_turns_since_summary >= s.memory_summary_trigger_turns or (
        conv.acc_tokens_since_summary >= s.memory_summary_trigger_tokens
    )
    if not vol:
        return False
    if prior_summary_count == 0:
        return True
    return conv.acc_turns_since_summary >= s.memory_summary_cooldown_turns or (
        conv.acc_tokens_since_summary >= s.memory_summary_cooldown_tokens
    )
