from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.orm import ChatMessage, Conversation, User, UserFeedback
from app.schemas.conversation import ChatMessageOut
from app.services import rate_limit_service
from app.services import chat_attachment_service
from app.services.distill_service import record_feedback
from app.utils.outbound_security import validate_public_http_url, validate_public_http_url_syntax


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/",
        "http://service.internal/",
        "file:///etc/passwd",
        "https://user:password@example.com/",
    ],
)
def test_ssrf_syntax_rejects_local_and_credential_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_http_url_syntax(url)


@pytest.mark.asyncio
async def test_ssrf_rejects_domain_resolving_to_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.10.0.5", 443))]

    monkeypatch.setattr("app.utils.outbound_security.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        await validate_public_http_url("https://example.test/path")


@pytest.mark.asyncio
async def test_rate_limit_memory_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit_service, "_redis_disabled_until", float("inf"))
    rate_limit_service._buckets.clear()
    assert (await rate_limit_service.consume("test", limit=2, window_seconds=60))[0]
    assert (await rate_limit_service.consume("test", limit=2, window_seconds=60))[0]
    assert not (await rate_limit_service.consume("test", limit=2, window_seconds=60))[0]


def test_attachment_id_cannot_escape_user_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(chat_attachment_service.settings, "chat_attachment_root", str(tmp_path))
    other = chat_attachment_service.user_dir("other") / "secret.txt"
    other.write_text("secret", encoding="utf-8")
    assert chat_attachment_service._resolve_attachment_path("user", "../other/*") is None


def test_chat_message_metadata_is_exposed_by_response_schema() -> None:
    row = ChatMessage(
        id="m1",
        conversation_id="c1",
        role="assistant",
        content="answer",
        trace_id="trace",
        citations_json=[{"title": "source"}],
        attachments_json=[{"id": "a1", "filename": "note.txt"}],
        tool_traces_json=[{"tool": "read_document", "ok": True}],
        created_at=datetime.now(timezone.utc),
    )
    output = ChatMessageOut.model_validate(row)
    assert output.citations == [{"title": "source"}]
    assert output.attachments == [{"id": "a1", "filename": "note.txt"}]
    assert output.tool_traces == [{"tool": "read_document", "ok": True}]


@pytest.mark.asyncio
async def test_feedback_uses_exact_question_link_instead_of_client_query() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        user = User(id="u1", email="feedback@example.com", password_hash="unused")
        conversation = Conversation(id="c1", user_id=user.id)
        question = ChatMessage(
            id="q1", conversation_id=conversation.id, role="user", content="真正的问题"
        )
        answer = ChatMessage(
            id="a1",
            conversation_id=conversation.id,
            role="assistant",
            content="回答",
            reply_to_message_id=question.id,
        )
        session.add_all([user, conversation, question, answer])
        await session.commit()

        await record_feedback(
            session,
            user_id=user.id,
            kb_id=None,
            conversation_id=conversation.id,
            message_id=answer.id,
            query_text="客户端传来的错误问题",
            correction="这里应该这样回答",
        )

        saved = (await session.execute(select(UserFeedback))).scalar_one()
        assert saved.message_id == answer.id
        assert saved.query_text == "真正的问题"

    await engine.dispose()
