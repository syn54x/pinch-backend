"""The production app shape, standing alone (M3 CP4 flushed this out).

Every other test runs inside the db fixture's ambient ferro session, which
hid a gap: the app itself never opened one, so a standalone process
(uvicorn, or the CLI tests' per-command lifespans) 500'd on its first
database operation. This test runs the app exactly as production does —
its own lifespan, no fixture session — and proves requests reach the
database through the app's own session management.
"""

from litestar.testing import AsyncTestClient

from pinch_backend.api.app import create_app
from pinch_backend.settings import settings


async def test_the_standalone_app_manages_its_own_database_sessions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:{tmp_path / 'standalone.db'}?mode=rwc")

    async with AsyncTestClient(create_app(), base_url="https://testserver.local") as client:
        await client.get("/health")
        response = await client.post(
            "/api/v1/auth/signup",
            json={"email": "taylor@example.com", "password": "correct horse battery staple"},
            headers={"x-csrftoken": client.cookies["csrftoken"]},
        )
        assert response.status_code == 201
        assert (await client.get("/api/v1/auth/me")).status_code == 200
