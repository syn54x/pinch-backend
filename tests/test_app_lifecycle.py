"""The production app shape, standing alone (M3 CP4 flushed this out).

Every other test runs inside the db fixture's ambient ferro session, which
hid a gap: the app itself never opened one, so a standalone process
(uvicorn, or the CLI tests' per-command lifespans) 500'd on its first
database operation. This test runs the app exactly as production does —
its own lifespan, no fixture session — and proves requests reach the
database through the app's own session management.
"""

from litestar.testing import AsyncTestClient

from pinch_backend.api import app as app_module
from pinch_backend.api.app import create_app
from pinch_backend.settings import settings


async def test_the_standalone_app_manages_its_own_database_sessions(
    standalone_db_url, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "database_url", standalone_db_url)

    async with AsyncTestClient(create_app(), base_url="https://testserver.local") as client:
        await client.get("/health")
        response = await client.post(
            "/api/v1/auth/signup",
            json={"email": "taylor@example.com", "password": "correct horse battery staple"},
            headers={"x-csrftoken": client.cookies["csrftoken"]},
        )
        assert response.status_code == 201
        assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_startup_applies_the_procrastinate_schema(standalone_db_url, monkeypatch) -> None:
    """Finding 12 (M5 CP4 PR review): a fresh environment where the API
    starts before any worker ever has must not rely on the worker to have
    applied Procrastinate's schema — the API's own lifecycle now calls
    ensure_job_schema too, right after open_job_app."""
    monkeypatch.setattr(settings, "database_url", standalone_db_url)
    calls = []
    original = app_module.ensure_job_schema

    async def _tracking() -> None:
        calls.append(True)
        await original()

    monkeypatch.setattr(app_module, "ensure_job_schema", _tracking)

    async with AsyncTestClient(create_app(), base_url="https://testserver.local") as client:
        await client.get("/health")

    assert calls == [True]
