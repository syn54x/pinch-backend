from litestar.testing import AsyncTestClient, TestClient


def test_import() -> None:
    import pinch_backend  # noqa: F401


def test_version() -> None:
    from pinch_backend import __version__

    assert __version__


def test_health() -> None:
    from pinch_backend.api.app import app

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_app_connects_database_on_startup() -> None:
    """PRD M1: the API process connects (and auto-migrates in development)
    on startup — domain models are queryable with no ceremony."""
    from ferro import engines

    from pinch_backend.api.app import app
    from pinch_backend.models import Ledger

    async with AsyncTestClient(app), engines.session():
        assert await Ledger.select().count() == 0
