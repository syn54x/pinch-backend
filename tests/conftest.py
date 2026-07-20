import os
import uuid

import pytest
from ferro import connect, engines, execute, reset_engine

DEFAULT_TEST_DATABASE_URL = "postgres://postgres:password@localhost:5432/postgres"
"""The local-pg docker container; CI's service container answers the same
DSN. sqlite was retired at M5 CP3 — Postgres is the only backend."""


def pytest_configure() -> None:
    os.environ.setdefault("LOGFIRE_SEND_TO_LOGFIRE", "false")
    os.environ.setdefault("PINCH_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    # No live network calls in CI (PRD M2): breach-check tests opt back in
    # through a stubbed transport.
    os.environ.setdefault("PINCH_BREACH_CHECK_ENABLED", "false")
    # Hermetic against the developer's .env (M7): pydantic-settings reads it,
    # but real Plaid credentials must never leak into the suite — "keyless"
    # is the tested baseline, and env vars outrank the file. The empty
    # string reads as unconfigured; tests that want Plaid monkeypatch the
    # settings object directly.
    os.environ["PINCH_PLAID_CLIENT_ID"] = ""
    os.environ["PINCH_PLAID_SECRET"] = ""
    os.environ["PINCH_SECRET_ENCRYPTION_KEY"] = ""


def _test_database_url() -> str:
    return os.environ.get("PINCH_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture
async def client(db):
    """The public HTTP seam (PRD M2 onward): the app over the per-test
    database. manage_database=False — the db fixture owns the connection.
    https base_url so the Secure session cookie survives the client's jar."""
    from litestar.testing import AsyncTestClient

    from pinch_backend.api.app import create_app

    async with AsyncTestClient(
        create_app(manage_database=False), base_url="https://testserver.local"
    ) as c:
        yield c


@pytest.fixture
async def db():
    """The model-layer seam: a real Postgres database per test, isolated via
    a throwaway schema (ferro_search_path).

    The import below registers every model table (domain + auth) before
    connect's auto-migration runs, so table creation never depends on which
    test module happened to import the app first. Deferred to fixture time
    because settings must load after pytest_configure's env defaults.
    """
    from pinch_backend import db as _db  # noqa: F401

    postgres_url = _test_database_url()
    schema = f"pinch_test_{uuid.uuid4().hex[:8]}"
    await connect(postgres_url)
    async with engines.session():
        await execute(f'CREATE SCHEMA "{schema}"')
    reset_engine()
    separator = "&" if "?" in postgres_url else "?"
    await connect(f"{postgres_url}{separator}ferro_search_path={schema}", auto_migrate=True)
    async with engines.session():
        yield
        await execute(f'DROP SCHEMA "{schema}" CASCADE')
    reset_engine()


@pytest.fixture
async def standalone_db_url():
    """A Postgres DSN carrying its own throwaway schema, for tests that run
    the app's OWN lifecycle (create_app() with manage_database=True) or the
    CLI's per-command lifespans, instead of the db fixture's ambient session."""
    from pinch_backend import db as _db  # noqa: F401

    postgres_url = _test_database_url()
    schema = f"pinch_standalone_{uuid.uuid4().hex[:8]}"
    await connect(postgres_url)
    async with engines.session():
        await execute(f'CREATE SCHEMA "{schema}"')
    reset_engine()
    separator = "&" if "?" in postgres_url else "?"
    yield f"{postgres_url}{separator}ferro_search_path={schema}"
    await connect(postgres_url)
    async with engines.session():
        await execute(f'DROP SCHEMA "{schema}" CASCADE')
    reset_engine()


@pytest.fixture(autouse=True)
def job_connector():
    """Every test runs Procrastinate on the in-memory connector — same
    stance as "no live network": nothing in the suite touches a real queue.
    Yields the connector; inspect queued jobs via `job_connector.jobs`."""
    from procrastinate import testing

    from pinch_backend.jobs import job_app

    in_memory = testing.InMemoryConnector()
    with job_app.replace_connector(in_memory):
        yield in_memory


@pytest.fixture
def run_jobs(job_connector):
    """Execute everything queued, then return (the testing-connector
    pattern): job effects are asserted back at the API seam."""
    from pinch_backend.jobs import job_app

    async def _run() -> None:
        await job_app.run_worker_async(
            wait=False, listen_notify=False, install_signal_handlers=False
        )

    return _run
