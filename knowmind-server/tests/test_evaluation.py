import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.services import evaluation_service as eval_svc


@pytest.fixture
async def async_client(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setenv("EVAL_REPORTS_DIR", str(reports))
    get_settings.cache_clear()

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
        yield client, reports
    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_evaluation_dashboard_empty(async_client) -> None:
    client, _reports = async_client
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": "eval@test.com", "password": "secret123"},
    )
    assert reg.status_code == 200
    token = reg.json()["access_token"]

    res = await client.get(
        "/api/v1/evaluation/dashboard",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "stub"
    assert body["stats"]["total_runs"] == 0


@pytest.mark.asyncio
async def test_evaluation_dashboard_reads_latest(async_client) -> None:
    client, reports = async_client
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": "eval2@test.com", "password": "secret123"},
    )
    token = reg.json()["access_token"]

    payload = {
        "run_id": "abc123",
        "created_at": "2026-05-27T10:00:00+00:00",
        "version": "v2.1.0",
        "mode": "simple",
        "sample_count": 5,
        "metrics": {
            "faithfulness": 0.89,
            "answer_relevancy": 0.92,
            "context_recall": 0.83,
            "context_precision": 0.86,
        },
        "deltas": {
            "faithfulness": 0.05,
            "answer_relevancy": 0.03,
            "context_recall": -0.01,
            "context_precision": 0.04,
        },
        "trend": [
            {
                "label": "5/27",
                "faithfulness": 89.0,
                "answer_relevancy": 92.0,
                "context_recall": 83.0,
                "context_precision": 86.0,
            }
        ],
        "version_compare": [
            {"name": "忠实度", "current": 89.0, "baseline": 84.0},
        ],
        "stats": {
            "total_runs": 3,
            "question_count": 5,
            "avg_latency_s": 1.9,
            "pass_rate": 0.94,
        },
    }
    (reports / "latest.json").write_text(json.dumps(payload), encoding="utf-8")

    res = await client.get(
        "/api/v1/evaluation/dashboard",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "simple"
    assert body["kpis"]["faithfulness"]["value"] == 0.89
    assert body["stats"]["total_runs"] == 3


def test_evaluation_service_repo_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVAL_REPORTS_DIR", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(eval_svc, "_reports_dir", lambda: tmp_path)
    out = eval_svc.get_dashboard()
    assert out.mode == "stub"
    get_settings.cache_clear()
