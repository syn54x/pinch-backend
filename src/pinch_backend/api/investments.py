"""/api/v1/investments — the investments domain surface (M10 CP0, issue
#73; PRD #72).

Read-only by law (ADR 0007): holdings are provider-owned with zero user
data, so this router is GETs and nothing else. Ledger-wide with an
account filter — one call serves a portfolio view, the filter serves an
account detail pane. Security identity is embedded; no standalone
securities endpoint exists until something needs it.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from litestar import Router, get
from litestar.di import NamedDependency
from litestar.params import QueryParameter
from pydantic import BaseModel, ConfigDict

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import Holding, Ledger, Security

AccountFilterParam = Annotated[
    uuid.UUID | None,
    QueryParameter(description="Restrict to one account's holdings."),
]


class SecurityOut(BaseModel):
    """A security's identity as embedded on every holding — an allowlist,
    never the row."""

    id: uuid.UUID
    name: str
    ticker_symbol: str | None
    type: str
    is_cash_equivalent: bool


class HoldingOut(BaseModel):
    """What a client may see about a position — an allowlist, never the
    row. ``institution_price`` is a per-share quote, not an Amount; every
    ``*_minor`` field follows the Money law."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    id: uuid.UUID
    account_id: uuid.UUID
    security: SecurityOut
    quantity: float
    institution_price: float | None
    institution_price_as_of: date | None
    institution_value_minor: int | None
    cost_basis_minor: int | None
    currency: str
    updated_at: datetime
    """When the last investments sync observed this position."""


def _holding_out(holding: Holding, security: Security) -> HoldingOut:
    return HoldingOut(
        id=holding.id,
        account_id=holding.account_id,  # ty: ignore[unresolved-attribute]
        security=SecurityOut(
            id=security.id,
            name=security.name,
            ticker_symbol=security.ticker_symbol,
            type=security.type,
            is_cash_equivalent=security.is_cash_equivalent,
        ),
        quantity=holding.quantity,
        institution_price=holding.institution_price,
        institution_price_as_of=holding.institution_price_as_of,
        institution_value_minor=holding.institution_value_minor,
        cost_basis_minor=holding.cost_basis_minor,
        currency=holding.currency,
        updated_at=holding.updated_at,
    )


@get("/holdings")
async def list_holdings(
    current_ledger: NamedDependency[Ledger],
    account_id: AccountFilterParam = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[HoldingOut]:
    """Current positions, ledger-fenced. A foreign or unknown account
    filter answers an empty page — filters filter; they never confirm."""
    ledger_id = current_ledger.id
    query = Holding.where(lambda h: h.ledger_id == ledger_id)
    if account_id is not None:
        query = query.where(lambda h, aid=account_id: h.account_id == aid)
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    security_ids = sorted({row.security_id for row in rows})  # ty: ignore[unresolved-attribute]
    securities: dict[uuid.UUID, Security] = {}
    if security_ids:
        for s in await Security.where(lambda s, ids=security_ids: s.id.in_(ids)).all():
            securities[s.id] = s
    return Page(
        items=[
            _holding_out(row, securities[row.security_id])  # ty: ignore[unresolved-attribute]
            for row in rows
        ],
        next_cursor=next_cursor,
    )


investments_router = Router(path="/api/v1/investments", route_handlers=[list_holdings])
