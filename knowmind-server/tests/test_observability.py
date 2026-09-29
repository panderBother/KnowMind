from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.orm import ChatMessage, Conversation, User
from app.services.observability_service import (
    add_usage_event,
    estimate_cost_microusd,
    estimate_prompt_tokens,
    get_usage_summary,
)


@pytest.mark.asyncio
async def test_usage_summary_groups_tokens_cost_and_latency(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.observability_service.settings.llm_input_cost_per_million_usd",
        1.0,
    )
    monkeypatch.setattr(
        "app.services.observability_service.settings.llm_output_cost_per_million_usd",
        2.0,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        session.add(User(id="usage-user", email="usage@test.local", password_hash="x"))
        session.add(Conversation(id="usage-conv", user_id="usage-user", title="usage"))
        session.add(
            ChatMessage(
                id="usage-message",
                conversation_id="usage-conv",
                role="assistant",
                content="answer",
            )
        )
        await session.flush()
        add_usage_event(
            session,
            user_id="usage-user",
            conversation_id="usage-conv",
            message_id="usage-message",
            trace_id="trace-1",
            model_mode="balanced",
            status="completed",
            input_tokens=1_000,
            output_tokens=500,
            latency_ms=900,
            tool_call_count=1,
        )
        add_usage_event(
            session,
            user_id="usage-user",
            conversation_id="usage-conv",
            message_id="usage-message",
            trace_id="trace-2",
            model_mode="fast",
            status="failed",
            input_tokens=500,
            output_tokens=0,
            latency_ms=100,
            error_type="TimeoutError",
        )
        await session.commit()

        summary = await get_usage_summary(session, "usage-user", days=7)

    await engine.dispose()
    assert summary["calls"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["total_tokens"] == 2_000
    assert summary["estimated_cost_usd"] == 0.0025
    assert summary["avg_latency_ms"] == 500
    assert summary["p95_latency_ms"] == 900
    assert summary["models"][0]["calls"] == 2


def test_token_and_cost_estimates(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.observability_service.settings.llm_input_cost_per_million_usd",
        1.0,
    )
    monkeypatch.setattr(
        "app.services.observability_service.settings.llm_output_cost_per_million_usd",
        2.0,
    )
    assert estimate_prompt_tokens([{"role": "user", "content": "中文测试"}]) > 0
    assert estimate_cost_microusd(1_000, 500) == 2_000
