import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture
async def async_client():
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
async def test_register_login_me_kb_flow(async_client: AsyncClient) -> None:
    reg = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "flow@example.com", "password": "password123"},
    )
    assert reg.status_code == 200, reg.text
    body = reg.json()
    token = body["access_token"]
    assert body["user"]["email"] == "flow@example.com"

    dup = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "flow@example.com", "password": "password123"},
    )
    assert dup.status_code == 409

    me = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == "flow@example.com"

    kbs = await async_client.get(
        "/api/v1/knowledge-bases",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert kbs.status_code == 200
    assert kbs.json() == []

    create = await async_client.post(
        "/api/v1/knowledge-bases",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "我的知识库"},
    )
    assert create.status_code == 201, create.text
    listed = await async_client.get(
        "/api/v1/knowledge-bases",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert len(listed.json()) == 1


@pytest.mark.asyncio
async def test_default_category_survives_after_list(async_client: AsyncClient) -> None:
    """GET categories 自动创建的「未分类」必须 commit，否则后续入库报「分类不存在」。"""
    reg = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "cat@example.com", "password": "password123"},
    )
    assert reg.status_code == 200, reg.text
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await async_client.post(
        "/api/v1/knowledge-bases",
        headers=headers,
        json={"name": "分类测试库"},
    )
    assert create.status_code == 201, create.text
    kb_id = create.json()["id"]

    listed = await async_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/categories",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    tree = listed.json()
    assert len(tree) >= 1
    assert tree[0]["name"] == "未分类"
    cat_id = tree[0]["id"]

    imported = await async_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/items/import-url",
        headers=headers,
        json={
            "url": "https://example.com",
            "category_id": cat_id,
            "publish": False,
            "title": "t",
            "content": "body",
        },
    )
    assert imported.status_code == 201, imported.text


@pytest.mark.asyncio
async def test_refresh_token_flow(async_client: AsyncClient) -> None:
    reg = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "refresh@example.com", "password": "password123"},
    )
    assert reg.status_code == 200, reg.text
    body = reg.json()
    refresh = body.get("refresh_token")
    assert refresh

    refreshed = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh},
    )
    assert refreshed.status_code == 200, refreshed.text
    new_body = refreshed.json()
    assert new_body["access_token"]
    assert new_body.get("refresh_token")

    me = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {new_body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == "refresh@example.com"
    assert new_body.get("refresh_token")


@pytest.mark.asyncio
async def test_logout_revokes_access_and_refresh_tokens(async_client: AsyncClient) -> None:
    reg = await async_client.post(
        "/api/v1/auth/register",
        json={"email": "logout@example.com", "password": "password123"},
    )
    assert reg.status_code == 200, reg.text
    body = reg.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    logout = await async_client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 204, logout.text

    old_access = await async_client.get("/api/v1/auth/me", headers=headers)
    assert old_access.status_code == 401
    old_refresh = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": body["refresh_token"]},
    )
    assert old_refresh.status_code == 401
