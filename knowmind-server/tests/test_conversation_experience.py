from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.orm import ChatMessage, Conversation
from app.services.chat_prefetch import sources_from_markdown


@pytest.fixture
async def conversation_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_enable_fk(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _register(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "conversation@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_conversation_search_pin_pagination_and_branch(conversation_client) -> None:
    client, factory = conversation_client
    headers = await _register(client)

    first = await client.post(
        "/api/v1/conversations",
        headers=headers,
        json={"title": "普通会话", "model_mode": "deep"},
    )
    second = await client.post(
        "/api/v1/conversations",
        headers=headers,
        json={"title": "另一条会话"},
    )
    assert first.status_code == second.status_code == 200
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    async with factory() as session:
        session.add_all(
            [
                ChatMessage(
                    id="question-1",
                    conversation_id=first_id,
                    role="user",
                    content="包含独特检索词",
                    sequence_no=1,
                ),
                ChatMessage(
                    id="answer-1",
                    conversation_id=first_id,
                    role="assistant",
                    content="第一轮回答",
                    sequence_no=2,
                    reply_to_message_id="question-1",
                    citations_json=[{"index": 1, "title": "来源", "snippet": "摘录"}],
                ),
                ChatMessage(
                    id="question-2",
                    conversation_id=first_id,
                    role="user",
                    content="第二轮问题",
                    sequence_no=3,
                ),
                ChatMessage(
                    id="answer-2",
                    conversation_id=first_id,
                    role="assistant",
                    content="第二轮回答",
                    sequence_no=4,
                    reply_to_message_id="question-2",
                    generation_status="generating",
                    created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        await session.commit()

    search = await client.get(
        "/api/v1/conversations",
        headers=headers,
        params={"q": "独特检索词", "main_chat_only": "true"},
    )
    assert search.status_code == 200
    assert [row["id"] for row in search.json()] == [first_id]

    pinned = await client.patch(
        f"/api/v1/conversations/{second_id}",
        headers=headers,
        json={"is_pinned": True},
    )
    assert pinned.status_code == 200
    assert pinned.json()["is_pinned"] is True

    page = await client.get(
        "/api/v1/conversations",
        headers=headers,
        params={"limit": 1, "offset": 0, "main_chat_only": "true"},
    )
    assert page.status_code == 200
    assert [row["id"] for row in page.json()] == [second_id]

    branch = await client.post(
        f"/api/v1/conversations/{first_id}/branch",
        headers=headers,
        json={"from_message_id": "answer-2", "title": "回答分支"},
    )
    assert branch.status_code == 200
    payload = branch.json()
    assert payload["parent_conversation_id"] == first_id
    assert payload["branched_from_message_id"] == "answer-2"
    assert payload["model_mode"] == "deep"

    branch_messages = await client.get(
        f"/api/v1/conversations/{payload['id']}/messages", headers=headers
    )
    assert branch_messages.status_code == 200
    assert [row["content"] for row in branch_messages.json()] == [
        "包含独特检索词",
        "第一轮回答",
    ]
    assert branch_messages.json()[1]["citations"][0]["title"] == "来源"

    recovered = await client.get(
        f"/api/v1/conversations/{first_id}/messages", headers=headers
    )
    assert recovered.status_code == 200
    assert recovered.json()[-1]["generation_status"] == "stopped"

    async with factory() as session:
        source = await session.get(Conversation, first_id)
        assert source is not None
        source_rows = list(
            (
                await session.execute(
                    select(ChatMessage).where(ChatMessage.conversation_id == first_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(source_rows) == 4


def test_external_sources_are_normalized_for_unified_panel() -> None:
    markdown = """## 联网搜索结果

### [1] 示例标题
- 链接：https://example.com/a
- 摘要：联网摘要

### [2] 论文标题
- **作者**：某作者
- **链接**：https://example.com/paper

论文摘要正文
"""
    sources = sources_from_markdown(markdown, "web")
    assert sources == [
        {
            "index": 1,
            "title": "示例标题",
            "snippet": "联网摘要",
            "source_type": "web",
            "url": "https://example.com/a",
        },
        {
            "index": 2,
            "title": "论文标题",
            "snippet": "论文摘要正文",
            "source_type": "web",
            "url": "https://example.com/paper",
        },
    ]


def test_conversation_experience_migration_backfills_message_sequence() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "016_conversation_experience.py"
    )
    spec = importlib.util.spec_from_file_location("conversation_experience_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE conversations ("
                "id VARCHAR(36) PRIMARY KEY, title VARCHAR(255), created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE chat_messages ("
                "id VARCHAR(36) PRIMARY KEY, conversation_id VARCHAR(36), created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO chat_messages (id, conversation_id, created_at) VALUES "
                "('m2', 'c1', '2026-01-01 00:00:02'), "
                "('m1', 'c1', '2026-01-01 00:00:01'), "
                "('m3', 'c2', '2026-01-01 00:00:01')"
            )
        )

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        message_columns = {column["name"] for column in inspect(connection).get_columns("chat_messages")}
        conversation_columns = {
            column["name"] for column in inspect(connection).get_columns("conversations")
        }
        assert {"sequence_no", "generation_status"} <= message_columns
        assert {
            "parent_conversation_id",
            "branched_from_message_id",
            "is_pinned",
            "model_mode",
        } <= conversation_columns
        rows = connection.execute(
            text(
                "SELECT id, sequence_no FROM chat_messages "
                "WHERE conversation_id = 'c1' ORDER BY sequence_no"
            )
        ).all()
        assert rows == [("m1", 1), ("m2", 2)]

    engine.dispose()
