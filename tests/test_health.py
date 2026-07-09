from litestar.testing import TestClient


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
