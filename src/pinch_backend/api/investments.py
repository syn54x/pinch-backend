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
    paginate_by_date,
)
from pinch_backend.models import (
    Holding,
    InvestmentActivity,
    InvestmentActivityType,
    Ledger,
    Security,
)

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


def _security_out(security: Security) -> SecurityOut:
    return SecurityOut(
        id=security.id,
        name=security.name,
        ticker_symbol=security.ticker_symbol,
        type=security.type,
        is_cash_equivalent=security.is_cash_equivalent,
    )


def _holding_out(holding: Holding, security: Security) -> HoldingOut:
    assert holding.account_id is not None
    return HoldingOut(
        id=holding.id,
        account_id=holding.account_id,
        security=_security_out(security),
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
    security_ids = sorted({row.security_id for row in rows})
    securities: dict[uuid.UUID, Security] = {}
    if security_ids:
        for s in await Security.where(lambda s, ids=security_ids: s.id.in_(ids)).all():
            securities[s.id] = s
    items = []
    for row in rows:
        assert row.security_id is not None
        items.append(_holding_out(row, securities[row.security_id]))
    return Page(items=items, next_cursor=next_cursor)


class InvestmentActivityOut(BaseModel):
    """What a client may see about an activity — an allowlist, never the
    row. Never reviewed, categorized, or transfer-linked (ADR 0007): the
    shape carries no user-data fields because none exist."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    id: uuid.UUID
    account_id: uuid.UUID
    security: SecurityOut | None
    """Absent on securityless cash events (account fees, plain deposits)."""
    date: date
    name: str
    amount_minor: int
    quantity: float
    price: float | None
    fees_minor: int | None
    type: InvestmentActivityType
    subtype: str | None
    currency: str


def _activity_out(activity: InvestmentActivity, security: Security | None) -> InvestmentActivityOut:
    assert activity.account_id is not None
    return InvestmentActivityOut(
        id=activity.id,
        account_id=activity.account_id,
        security=None if security is None else _security_out(security),
        date=activity.date,
        name=activity.name,
        amount_minor=activity.amount_minor,
        quantity=activity.quantity,
        price=activity.price,
        fees_minor=activity.fees_minor,
        type=activity.type,
        subtype=activity.subtype,
        currency=activity.currency,
    )


@get("/activities")
async def list_investment_activities(
    current_ledger: NamedDependency[Ledger],
    account_id: AccountFilterParam = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[InvestmentActivityOut]:
    """The activity feed, newest first (the transactions-list keyset),
    ledger-fenced with the same non-confirming account filter."""
    ledger_id = current_ledger.id
    query = InvestmentActivity.where(lambda x: x.ledger_id == ledger_id)
    if account_id is not None:
        query = query.where(lambda x, aid=account_id: x.account_id == aid)
    rows, next_cursor = await paginate_by_date(query, cursor=cursor, limit=limit)
    security_ids = sorted({row.security_id for row in rows if row.security_id is not None})
    securities: dict[uuid.UUID, Security] = {}
    if security_ids:
        for s in await Security.where(lambda s, ids=security_ids: s.id.in_(ids)).all():
            securities[s.id] = s
    return Page(
        items=[_activity_out(row, securities.get(row.security_id)) for row in rows],
        next_cursor=next_cursor,
    )


investments_router = Router(
    path="/api/v1/investments", route_handlers=[list_holdings, list_investment_activities]
)
