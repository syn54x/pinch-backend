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
    await ferro.connect(
        settings.database_url,
        auto_migrate=settings.database_auto_migrate,
        migrate_updates=settings.database_migrate_updates,
        migrate_destructive=settings.database_migrate_destructive,
    )
    async with ferro.engines.session():
        await backfill_accepted_untouched()


async def backfill_accepted_untouched() -> None:
    """One-shot, idempotent (F4 Enabler A, #66): pre-F4 DECISION rows carry
    accepted_untouched=NULL; the rows are self-contained, so the flag is
    derivable. One approximation, documented: proposed_transfer was never
    snapshotted, so a transfer decision counts as untouched only under
    DETECTION provenance (the only provenance whose accept path minted a
    transfer on this row). Void rows stay NULL — not decisions."""
    from pinch_backend.models import CorrectionKind, CorrectionLogEntry, ProposalProvenance

    rows = await CorrectionLogEntry.where(
        lambda e: (e.accepted_untouched == None) & (e.kind == CorrectionKind.DECISION)  # noqa: E711
    ).all()
    for e in rows:
        e.accepted_untouched = (
            e.decision_splits is None
            and e.decision_category_id == e.proposal_category_id
            and set(e.decision_tags) == set(e.proposal_tags)
            and (e.decision_display_name in (None, e.proposal_display_name))
            and (
                e.decision_transfer is None or e.proposal_provenance is ProposalProvenance.DETECTION
            )
        )
        await e.save()


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
