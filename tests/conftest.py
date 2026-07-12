import os
import uuid

import pytest
from ferro import connect, engines, execute, reset_engine


def pytest_configure() -> None:
    os.environ.setdefault("LOGFIRE_SEND_TO_LOGFIRE", "false")
    os.environ.setdefault("PINCH_DATABASE_URL", "sqlite::memory:")


@pytest.fixture
async def db(tmp_path):
    """The model-layer seam: a real database per test.

    sqlite by default; Postgres when PINCH_TEST_DATABASE_URL is set, isolated
    per test via a throwaway schema (ferro's backend-matrix convention in
    miniature).
    """
    postgres_url = os.environ.get("PINCH_TEST_DATABASE_URL")
    if postgres_url:
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
    else:
        await connect(f"sqlite:{tmp_path / 'pinch_test.db'}?mode=rwc", auto_migrate=True)
        async with engines.session():
            yield
        reset_engine()
