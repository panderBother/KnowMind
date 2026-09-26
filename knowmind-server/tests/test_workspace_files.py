from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.core.config import settings


@pytest.fixture
async def async_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "user_workspace_root", str(tmp_path))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_enable_fk(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_workspace_write_read_flow(async_client: AsyncClient, tmp_path: Path) -> None:
    reg = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "files@example.com", "password": "secret123"},
    )
    assert reg.status_code == 200
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    target = "note.md"
    wr = await async_client.post(
        "/api/v1/workspace/files/write",
        headers=headers,
        json={"path": target, "content": "# hello\n", "format": "auto"},
    )
    assert wr.status_code == 200
    assert wr.json()["status"] == "written"
    assert Path(wr.json()["path"]).read_text(encoding="utf-8") == "# hello\n"

    rd = await async_client.post(
        "/api/v1/workspace/files/read",
        headers=headers,
        json={"path": target},
    )
    assert rd.status_code == 200
    assert "# hello" in rd.json()["content"]

    roots = await async_client.get("/api/v1/workspace/files/roots", headers=headers)
    assert roots.status_code == 200
    assert any(str(tmp_path) in r for r in roots.json()["allowed_roots"])

    escaped = await async_client.post(
        "/api/v1/workspace/files/write",
        headers=headers,
        json={"path": "../escape.md", "content": "blocked", "format": "auto"},
    )
    assert escaped.status_code == 400

    other_reg = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "other-files@example.com", "password": "secret123"},
    )
    assert other_reg.status_code == 200
    other_headers = {"Authorization": f"Bearer {other_reg.json()['access_token']}"}
    other_read = await async_client.post(
        "/api/v1/workspace/files/read",
        headers=other_headers,
        json={"path": target},
    )
    assert other_read.status_code == 400
