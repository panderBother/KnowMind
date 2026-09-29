from __future__ import annotations


from sqlalchemy.ext.asyncio import AsyncSession

from app.services.conversation_service import get_conversation_for_user, load_messages_ordered
from app.services.distill_service import DistillError
from app.services.edgefn_client import complete_chat_turn, turn_visible_text
from app.services.knowledge_category_service import ensure_default_category
from app.services.knowledge_item_service import create_item
from app.utils.llm_json import parse_llm_json_array


async def extract_knowledge_drafts(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    kb_id: str,
    message_limit: int = 8,
) -> list[dict]:
    await get_conversation_for_user(session, conversation_id=conversation_id, user_id=user_id)
    rows = await load_messages_ordered(session, conversation_id)
    if not rows:
        raise DistillError("会话无消息", 400)
    tail = rows[-message_limit:]
    lines: list[str] = []
    for m in tail:
        role = "用户" if m.role == "user" else "助手"
        lines.append(f"**{role}**：{m.content[:2000]}")
    transcript = "\n\n".join(lines)

    prompt = f"""从下列对话中提炼 1-3 条可写入知识库的独立知识点。
输出**仅** JSON 数组：
[{{"title":"...", "content":"...", "tags":["..."]}}]
- content 用 Markdown
- 勿编造对话未出现的事实
- 中文

对话摘录：
{transcript}
"""
    turn = await complete_chat_turn([{"role": "user", "content": prompt}])
    text = turn_visible_text(turn) or (turn.content or turn.reasoning or "").strip()
    drafts = parse_llm_json_array(text)
    if drafts is None:
        raise DistillError("LLM 未返回有效 JSON")
    if not isinstance(drafts, list):
        raise DistillError("格式错误")
    out: list[dict] = []
    for d in drafts[:5]:
        if not isinstance(d, dict):
            continue
        title = str(d.get("title") or "").strip()
        content = str(d.get("content") or "").strip()
        if not title or not content:
            continue
        tags = d.get("tags") if isinstance(d.get("tags"), list) else []
        out.append({"title": title[:200], "content": content, "tags": [str(t) for t in tags][:10]})
    if not out:
        raise DistillError("未提炼出有效条目")
    return out


async def import_drafts(
    session: AsyncSession,
    user_id: str,
    kb_id: str,
    drafts: list[dict],
    *,
    publish: bool = False,
) -> list[dict]:
    cat = await ensure_default_category(session, user_id, kb_id)
    created: list[dict] = []
    for d in drafts:
        title = str(d.get("title") or "").strip()
        content = str(d.get("content") or "").strip()
        if not title or not content:
            continue
        tags = d.get("tags") if isinstance(d.get("tags"), list) else []
        item = await create_item(
            session,
            user_id,
            kb_id,
            title=title,
            content=content,
            category_id=cat.id,
            tags=[str(t) for t in tags][:10],
            source="conversation_extract",
            source_type="ai_extract",
            publish=publish,
        )
        await session.flush()
        created.append(
            {
                "id": item.id,
                "title": item.title,
                "lifecycle_status": item.lifecycle_status,
            }
        )
    await session.commit()
    return created
