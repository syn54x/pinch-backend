"""Database lifecycle for the API process (connect on startup, PRD M1)."""

import ferro

from pinch_backend import models  # noqa: F401 — register domain models before connect
from pinch_backend.settings import settings


async def connect_database() -> None:
    await ferro.connect(settings.database_url, auto_migrate=settings.database_auto_migrate)


async def disconnect_database() -> None:
    ferro.reset_engine()
