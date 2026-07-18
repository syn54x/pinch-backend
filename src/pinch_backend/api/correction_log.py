"""/api/v1/correction-log — the append-only decision record, readable
(PRD M5 #21): the parity principle applied; M9's eval export is a consumer
of this endpoint. Read-only — entries are written by consume/undo, never
over HTTP."""

import uuid
from datetime import date, datetime
from typing import Annotated

from litestar import Router, get
from litestar.di import NamedDependency
from litestar.params import QueryParameter
from pydantic import BaseModel

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    ProposalProvenance,
)


class CorrectionLogEntryOut(BaseModel):
    """One log entry — an allowlist mirror of the wide, self-contained row."""

    id: uuid.UUID
    transaction_id: uuid.UUID
    kind: CorrectionKind
    actor: CorrectionActor
    input_description_raw: str | None
    input_payee: str | None
    input_amount_minor: int | None
    input_currency: str | None
    input_date: date | None
    input_account_id: uuid.UUID | None
    proposal_category_id: uuid.UUID | None
    proposal_category_name: str | None
    proposal_tags: list[str]
    proposal_display_name: str | None
    proposal_provenance: ProposalProvenance | None
    proposal_detail: dict | None
    decision_category_id: uuid.UUID | None
    decision_category_name: str | None
    decision_tags: list[str]
    decision_display_name: str | None
    decision_splits: list[dict] | None
    decision_transfer: dict | None
    voids: uuid.UUID | None
    void_reason: str | None
    created_at: datetime


def _out(e: CorrectionLogEntry) -> CorrectionLogEntryOut:
    return CorrectionLogEntryOut(
        id=e.id,
        transaction_id=e.transaction_id,
        kind=e.kind,
        actor=e.actor,
        input_description_raw=e.input_description_raw,
        input_payee=e.input_payee,
        input_amount_minor=e.input_amount_minor,
        input_currency=e.input_currency,
        input_date=e.input_date,
        input_account_id=e.input_account_id,
        proposal_category_id=e.proposal_category_id,
        proposal_category_name=e.proposal_category_name,
        proposal_tags=e.proposal_tags,
        proposal_display_name=e.proposal_display_name,
        proposal_provenance=e.proposal_provenance,
        proposal_detail=e.proposal_detail,
        decision_category_id=e.decision_category_id,
        decision_category_name=e.decision_category_name,
        decision_tags=e.decision_tags,
        decision_display_name=e.decision_display_name,
        decision_splits=e.decision_splits,
        decision_transfer=e.decision_transfer,
        voids=e.voids,
        void_reason=e.void_reason,
        created_at=e.created_at,
    )


@get("/")
async def list_correction_log(
    current_ledger: NamedDependency[Ledger],
    transaction_id: Annotated[uuid.UUID | None, QueryParameter()] = None,
    actor: Annotated[CorrectionActor | None, QueryParameter()] = None,
    kind: Annotated[CorrectionKind | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[CorrectionLogEntryOut]:
    ledger_id = current_ledger.id
    query = CorrectionLogEntry.where(lambda e: e.ledger_id == ledger_id)
    if transaction_id is not None:
        tid = transaction_id
        query = query.where(lambda e, tid=tid: e.transaction_id == tid)
    if actor is not None:
        wanted_actor = actor
        query = query.where(lambda e, a=wanted_actor: e.actor == a)
    if kind is not None:
        wanted_kind = kind
        query = query.where(lambda e, k=wanted_kind: e.kind == k)
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    return Page(items=[_out(e) for e in rows], next_cursor=next_cursor)


correction_log_router = Router(path="/api/v1/correction-log", route_handlers=[list_correction_log])
