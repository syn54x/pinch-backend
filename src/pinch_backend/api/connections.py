"""/api/v1/connections — Plaid connections (M7 CP1, issue #33; PRD #31).

The connect flow: link token → (widget, or sandbox shortcut) → public-token
exchange, which creates the Connection and one Account per consented Plaid
account — no second selection layer; Link's widget already did selection.
The access token is encrypted at rest and write-only at this surface.

Keyless instances (no Plaid settings) refuse the Plaid-touching endpoints
cleanly and keep the health surface: connected-account support never holds
manual tracking hostage.

Absent by design, for now: DELETE (disconnect) is blocked on ferro-orm#325
(auto-migrate ignores on_delete alteration; CP0 findings on #32) — shipping
sever-not-destroy on a silently-CASCADE database would destroy. The initial
sync auto-enqueue arrives with the sync job (CP2, #34).
"""

import uuid
from datetime import datetime

from litestar import Router, get, post
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException, PermissionDeniedException
from litestar.params import FromPath
from pydantic import BaseModel

from pinch_backend import providers
from pinch_backend.api.accounts import AccountOut, _account_out
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.crypto import encrypt_secret
from pinch_backend.models import (
    Account,
    Connection,
    ConnectionProvider,
    ConnectionStatus,
    Ledger,
    User,
    transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)


class LinkTokenOut(BaseModel):
    link_token: str


class ConnectionCreateIn(BaseModel):
    public_token: str


class ConnectionOut(BaseModel):
    """What a client may see about a connection — an allowlist, never the
    row, and never the access token in any form."""

    id: uuid.UUID
    provider: ConnectionProvider
    status: ConnectionStatus
    last_synced_at: datetime | None
    error_detail: str | None
    accounts: list[AccountOut]
    created_at: datetime


def _require_plaid() -> None:
    """The keyless stance (PRD #31): a clean refusal on the endpoints that
    would touch Plaid, and nothing else changes."""
    if not settings.plaid_configured:
        raise PermissionDeniedException(detail="Plaid is not configured on this instance")


async def _connection_out(connection: Connection) -> ConnectionOut:
    cid = connection.id
    accounts = await Account.where(lambda a: a.connection_id == cid).all()
    return ConnectionOut(
        id=connection.id,
        provider=connection.provider,
        status=connection.status,
        last_synced_at=connection.last_synced_at,
        error_detail=connection.error_detail,
        accounts=[await _account_out(a) for a in accounts],
        created_at=connection.created_at,
    )


@post("/link-token")
async def create_link_token(current_user: NamedDependency[User]) -> LinkTokenOut:
    """Shaped for a future frontend to drop Plaid Link on top unchanged;
    sandbox tests shortcut the widget between this and the exchange."""
    _require_plaid()
    token = await providers.get_provider().create_link_token(client_user_id=str(current_user.id))
    return LinkTokenOut(link_token=token)


@post("/")
async def create_connection(
    data: ConnectionCreateIn,
    current_user: NamedDependency[User],
    current_ledger: NamedDependency[Ledger],
) -> ConnectionOut:
    """Exchange the public token; create the Connection and one Account per
    account on the Item, atomically. Currency falls back to the acting
    user's primary currency when the provider omits it."""
    _require_plaid()
    provider = providers.get_provider()
    exchanged = await provider.exchange_public_token(data.public_token)
    provider_accounts = await provider.get_accounts(exchanged.access_token)
    async with transaction():
        connection = await Connection.create(
            ledger=current_ledger,
            provider=ConnectionProvider.PLAID,
            provider_item_id=exchanged.item_id,
            status=ConnectionStatus.ACTIVE,
            encrypted_secret=encrypt_secret(exchanged.access_token),
        )
        for pa in provider_accounts:
            await Account.create(
                ledger=current_ledger,
                kind=pa.kind,
                label=pa.name,
                currency=pa.currency or current_user.primary_currency,
                connection=connection,
                provider_account_id=pa.provider_account_id,
            )
    log.info(
        "connection.created",
        connection_id=str(connection.id),
        ledger_id=str(connection.ledger_id),  # ty: ignore[unresolved-attribute]
        account_count=len(provider_accounts),
    )
    return await _connection_out(connection)


@get("/")
async def list_connections(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[ConnectionOut]:
    """The health surface (PRD #31 story 21): status, last sync, error
    detail, accounts — the CLI's connection view is this list."""
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        Connection.where(lambda c: c.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(items=[await _connection_out(c) for c in rows], next_cursor=next_cursor)


@get("/{connection_id:uuid}")
async def get_connection(
    connection_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> ConnectionOut:
    connection = await Connection.where(
        lambda c: (c.id == connection_id) & (c.ledger_id == current_ledger.id)
    ).first()
    if connection is None:
        raise NotFoundException(detail="No such connection")
    return await _connection_out(connection)


connections_router = Router(
    path="/api/v1/connections",
    route_handlers=[create_link_token, create_connection, list_connections, get_connection],
)
