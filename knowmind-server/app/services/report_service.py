from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import ResearchReport, new_uuid
from app.services.conversation_service import get_conversation_for_user, load_messages_ordered
from app.services.edgefn_client import ChatTurnResult, complete_chat_turn
from app.services.knowledge_item_service import _ensure_kb
from app.services.rag_context import search_kb
from app.http_client import friendly_connect_error
from app.services.rag_citations import hits_to_source_payload, resolve_hit_titles
from app.services.rag_logging_service import log_rag_retrieval

log = logging.getLogger(__name__)

_MAX_REPORT_PROMPT_CHARS = 28_000


class ReportError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _extract_outline(md: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^##\s+(.+)$", md, re.MULTILINE) if m.strip()]


def _extract_title_from_md(md: str, fallback: str) -> tuple[str, str]:
    """返回 (title, body_without_h1)。"""
    lines = md.strip().splitlines()
    if lines and re.match(r"^#\s+", lines[0]):
        title = re.sub(r"^#\s+", "", lines[0]).strip()[:300]
        body = "\n".join(lines[1:]).lstrip()
        return title or fallback, body or md
    return fallback[:300], md


def _first_summary(md: str) -> str | None:
    for block in re.split(r"\n\s*\n", md):
        t = re.sub(r"^#{1,6}\s+", "", block.strip())
        t = re.sub(r"\s+", " ", t)
        if len(t) >= 30 and not _is_garbage_text(t, min_len=30, min_headers=0):
            return t[:500]
    return None


_THINKING_TAGS = ("think", "redacted_thinking", "reasoning")

_PLANNING_MARKERS = (
    "我收到了",
    "硬性要求",
    "让我先",
    "用户的请求",
    "我需要",
    "首先分析",
    "脚注编号",
    "Markdown格式",
    "撰写一份",
    "知识库摘录",
    "必须对应",
    "研究报告的要求",
    "让我梳理",
    "接下来",
    "条引用",
)


def _strip_thinking_blocks(text: str) -> str:
    out = text.strip()
    for tag in _THINKING_TAGS:
        open_pat = rf"<{tag}\b[^>]*>"
        close_pat = rf"</{tag}>"
        out = re.sub(open_pat + r"[\s\S]*?" + close_pat, "", out, flags=re.IGNORECASE)
    for tag in _THINKING_TAGS:
        m = re.search(rf"</{tag}>\s*", out, re.IGNORECASE)
        if m:
            tail = out[m.end() :].strip()
            if tail:
                return tail
    for tag in _THINKING_TAGS:
        out = re.sub(rf"<{tag}\b[^>]*>[\s\S]*", "", out, flags=re.IGNORECASE)
    return out.strip()


def _is_planning_monologue(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    hits = sum(1 for m in _PLANNING_MARKERS if m in t)
    h2_count = len(re.findall(r"^##\s+", t, re.MULTILINE))
    h1_count = len(re.findall(r"^#\s+", t, re.MULTILINE))
    if re.search(r"<(?:think|redacted_thinking|reasoning)\b", t, re.IGNORECASE) and h2_count < 2:
        return True
    if hits >= 2 and h2_count < 2:
        return True
    if hits >= 3:
        return True
    if hits >= 1 and h2_count == 0 and h1_count == 0:
        return True
    return False


def finalize_report_markdown(text: str) -> str:
    """剥离推理链 / 规划语，只保留 Markdown 报告正文。"""
    if not text:
        return ""
    t = _strip_thinking_blocks(text)
    m = re.search(r"(##\s*研究背景[\s\S]*)", t)
    if m:
        blob = m.group(1).strip()
        if not _is_planning_monologue(blob):
            return blob
    m = re.search(r"(^#\s+.+(?:\n(?!#\s).+)*)", t, re.MULTILINE)
    if m:
        chunk = m.group(1).strip()
        if "##" in chunk and not _is_planning_monologue(chunk):
            return chunk
    m2 = re.search(r"^##\s+", t, re.MULTILINE)
    if m2:
        tail = t[m2.start() :].strip()
        if tail and not _is_planning_monologue(tail):
            return tail
    m3 = re.search(r"^#\s+", t, re.MULTILINE)
    if m3:
        tail = t[m3.start() :].strip()
        if tail and not _is_planning_monologue(tail):
            return tail
    if _is_planning_monologue(t):
        return ""
    return t.strip()


def _is_garbage_text(text: str, *, min_len: int = 200, min_headers: int = 2) -> bool:
    t = (text or "").strip()
    if len(t) < min_len:
        return True
    if min_headers and t.count("##") < min_headers:
        return True
    words = re.findall(r"[\w\u4e00-\u9fff]+", t)
    if len(words) >= 8:
        word, cnt = Counter(words).most_common(1)[0]
        ratio = cnt / len(words)
        # 英文/短词刷屏（如 Sam Sam Sam）
        if ratio > 0.35 and word.isascii() and len(word) <= 6:
            return True
    return False


def _extract_markdown_report_blob(text: str) -> str:
    """从混杂推理链的文本中截取 Markdown 报告段。"""
    cleaned = _strip_thinking_blocks(text)
    for src in (cleaned, text.strip()):
        m = re.search(r"(^#\s+.+(?:\n(?!#\s).+)*)", src, re.MULTILINE)
        if m:
            chunk = m.group(1).strip()
            if "##" in chunk:
                return chunk
        m2 = re.search(r"(##\s+研究背景[\s\S]+)", src)
        if m2:
            return m2.group(1).strip()
    return cleaned


def _pick_report_markdown(turn: ChatTurnResult) -> str:
    candidates: list[str] = []
    for raw in ((turn.content or "").strip(), (turn.reasoning or "").strip()):
        if not raw:
            continue
        candidates.append(finalize_report_markdown(_extract_markdown_report_blob(raw)))
        candidates.append(finalize_report_markdown(raw))
    seen: set[str] = set()
    for c in candidates:
        c = c.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        if _is_planning_monologue(c):
            continue
        if not _is_garbage_text(c):
            return c
    for c in candidates:
        c = (c or "").strip()
        if c and not _is_planning_monologue(c):
            return c
    return ""


async def _call_report_llm(messages: list[dict[str, str]]) -> ChatTurnResult:
    try:
        return await complete_chat_turn(messages)
    except RuntimeError as e:
        log.warning("报告生成 LLM 调用失败: %s", e)
        raise ReportError(friendly_connect_error(e), 502) from e


async def _generate_report_markdown(messages: list[dict[str, str]]) -> tuple[str, ChatTurnResult]:
    turn = await _call_report_llm(messages)
    md = _pick_report_markdown(turn)
    if not _is_garbage_text(md):
        return md, turn

    log.warning(
        "报告初稿不合格 content_len=%s reasoning_len=%s preview=%s",
        len(turn.content or ""),
        len(turn.reasoning or ""),
        (md or "")[:120],
    )
    retry = messages + [
        {
            "role": "user",
            "content": (
                "上次输出无效。请**只**输出 Markdown 报告：第一行 `# 标题`；"
                "必须包含 `## 研究背景`、`## 核心发现`、`## 结论与展望`；"
                "中文正文 800 字以上；禁止输出无意义重复词或英文占位符。"
            ),
        },
    ]
    turn2 = await _call_report_llm(retry)
    md2 = _pick_report_markdown(turn2)
    if md2 and not _is_garbage_text(md2):
        return md2, turn2
    raise ReportError(
        "模型未生成合格报告正文。建议将 EDGEFN_CHAT_MODEL 换为非推理模型（如 Qwen3-8B），或缩短对话后再试。",
        502,
    )


async def generate_report_from_conversation(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    *,
    kb_id: str,
    title_override: str | None = None,
) -> ResearchReport:
    conv = await get_conversation_for_user(session, conversation_id=conversation_id, user_id=user_id)
    await _ensure_kb(session, user_id, kb_id)
    if conv.knowledge_base_id and conv.knowledge_base_id != kb_id:
        pass  # 允许显式指定 kb，与对话所选库可不同

    rows = await load_messages_ordered(session, conversation_id)
    if not rows:
        raise ReportError("会话无消息，无法生成报告", 400)

    user_msgs = [m for m in rows if m.role == "user"]
    assistant_msgs = [m for m in rows if m.role == "assistant"]
    query = (user_msgs[-1].content if user_msgs else rows[-1].content).strip()[:4000]
    raw_answer = (assistant_msgs[-1].content if assistant_msgs else "").strip() or None

    tail = rows[-12:]
    transcript_lines: list[str] = []
    for m in tail:
        role = "用户" if m.role == "user" else "助手"
        transcript_lines.append(f"**{role}**：{m.content[:2500]}")
    transcript = "\n\n".join(transcript_lines)

    rag = await search_kb(session, user_id, kb_id, query)
    await log_rag_retrieval(
        session,
        user_id=user_id,
        kb_id=kb_id,
        query=query,
        conversation_id=conversation_id,
        hits=rag.hits,
    )
    titles = await resolve_hit_titles(session, rag.hits)
    citations = hits_to_source_payload(rag.hits, titles)

    evidence = rag.markdown or "（未检索到知识库摘录）"
    prompt = f"""请基于下列对话与知识库摘录，撰写一篇中文 Markdown 研究报告。

硬性要求：
1. **第一行**必须是 `# 报告标题`（简短、准确，勿用「报告」二字敷衍）
2. 必须包含章节：`## 研究背景`、`## 核心发现`、`## 结论与展望`（可按主题增加其它 `##` 小节）
3. 论断须用脚注 [^1] [^2] … 标注，编号与下方「知识库摘录」中的「摘录 N」一致（最多 {len(citations)} 个）
4. 勿编造对话与摘录中不存在的事实；无依据处可写「资料不足」
5. 正文约 800–2500 字，使用 Markdown 列表、加粗等排版

对话摘录：
{transcript}

知识库摘录（脚注来源，摘录序号对应 [^N]）：
{evidence}
"""
    if len(prompt) > _MAX_REPORT_PROMPT_CHARS:
        prompt = prompt[:_MAX_REPORT_PROMPT_CHARS] + "\n\n…（上下文过长已截断）"

    messages = [
        {
            "role": "system",
            "content": (
                "你是报告撰写助手。直接输出最终 Markdown 报告。"
                "禁止输出思考过程、禁止输出 think/redacted_thinking/reasoning 等 XML 标签或任何规划说明。"
                "第一行必须是 `# 标题`，且正文须含多个 `##` 章节。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    raw_md, turn = await _generate_report_markdown(messages)
    raw_md = finalize_report_markdown(raw_md)
    if not raw_md.strip() or _is_planning_monologue(raw_md):
        raise ReportError("模型未返回报告正文", 502)

    fallback_title = (title_override or conv.title or query[:80] or "研究报告").strip()
    title, body_md = _extract_title_from_md(raw_md, fallback_title)
    body_md = finalize_report_markdown(body_md)
    if title_override:
        title = title_override.strip()[:300]

    outline = _extract_outline(body_md)
    summary = _first_summary(body_md)

    row = ResearchReport(
        id=new_uuid(),
        user_id=user_id,
        kb_id=kb_id,
        conversation_id=conversation_id,
        title=title,
        summary=summary,
        content_md=body_md[:50000],
        raw_answer_md=(raw_answer[:50000] if raw_answer else None)
        or ((turn.reasoning or turn.content or "")[:50000] or None),
        outline_json=outline,
        citations_json=citations,
        status="ready",
        updated_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_reports(
    session: AsyncSession,
    user_id: str,
    *,
    kb_id: str | None = None,
    limit: int = 50,
) -> list[ResearchReport]:
    lim = max(1, min(limit, 100))
    stmt = select(ResearchReport).where(ResearchReport.user_id == user_id)
    if kb_id:
        stmt = stmt.where(ResearchReport.kb_id == kb_id)
    stmt = stmt.order_by(ResearchReport.updated_at.desc()).limit(lim)
    q = await session.execute(stmt)
    return list(q.scalars().all())


async def get_report(session: AsyncSession, user_id: str, report_id: str) -> ResearchReport:
    row = await session.get(ResearchReport, report_id)
    if row is None or row.user_id != user_id:
        raise ReportError("报告不存在", 404)
    return row


async def delete_report(session: AsyncSession, user_id: str, report_id: str) -> None:
    row = await get_report(session, user_id, report_id)
    await session.delete(row)
    await session.commit()


def report_to_out(row: ResearchReport) -> dict:
    citations = row.citations_json if isinstance(row.citations_json, list) else []
    outline = row.outline_json if isinstance(row.outline_json, list) else []
    return {
        "id": row.id,
        "kb_id": row.kb_id,
        "conversation_id": row.conversation_id,
        "title": row.title,
        "summary": row.summary,
        "content_md": finalize_report_markdown(row.content_md or ""),
        "raw_answer_md": row.raw_answer_md,
        "outline": [str(x) for x in outline],
        "citations": citations,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def report_to_schema(row: ResearchReport):
    """ORM → Pydantic 响应（citations 转为 ReportCitationOut）。"""
    from app.schemas.report import ReportCitationOut, ResearchReportOut

    data = report_to_out(row)
    raw = data.pop("citations", [])
    return ResearchReportOut(
        **data,
        citations=[ReportCitationOut.model_validate(c) for c in raw],
    )
