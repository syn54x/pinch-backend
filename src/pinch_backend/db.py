"""Database lifecycle for the API process: connect on startup (PRD M1),
one ferro session per request (M3).
"""

from typing import TYPE_CHECKING

import ferro
from litestar.middleware.base import MiddlewareProtocol

from pinch_backend import models  # noqa: F401 — register domain models before connect
from pinch_backend.auth import models as auth_models  # noqa: F401 — register auth tables
from pinch_backend.settings import settings

if TYPE_CHECKING:
    from litestar.types import ASGIApp, Receive, Scope, Send


async def connect_database() -> None:
    await ferro.connect(settings.database_url, auto_migrate=settings.database_auto_migrate)


async def disconnect_database() -> None:
    ferro.reset_engine()


class FerroSessionMiddleware(MiddlewareProtocol):
    """One ferro session per HTTP request (ferro ≥0.13 routes operations
    through an explicit session, never an implicit default connection).

    Sessions nest, so tests that wrap themselves in an ambient session
    still work; in a standalone process this is the only session there is.
    """

    def __init__(self, app: "ASGIApp") -> None:
        self.app = app

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async with ferro.engines.session():
            await self.app(scope, receive, send)
