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

from litestar import Router, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException, PermissionDeniedException
from litestar.params import FromPath
from litestar.status_codes import HTTP_202_ACCEPTED, HTTP_502_BAD_GATEWAY
from pydantic import BaseModel

from pinch_backend import providers
from pinch_backend.api.accounts import AccountOut, account_out
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.crypto import decrypt_secret, encrypt_secret
from pinch_backend.jobs import sync_connection
from pinch_backend.models import (
    Account,
    Connection,
    ConnectionProvider,
    ConnectionStatus,
    Ledger,
    LedgerMember,
    LedgerRole,
    User,
    transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)


class LinkTokenIn(BaseModel):
    connection_id: uuid.UUID | None = None
    """Absent: a fresh connect. Present: an update-mode token repairing
    this connection's login (PRD #31 reauth) — no exchange follows; the
    next successful sync is the healer."""


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


def _surface(error: providers.ProviderError) -> Exception:
    """The recovery point for provider failures: the code — the only
    provider detail allowed out (PRD #31) — reaches the client instead of
    an opaque 500. A rejected public token is the client's fault (400);
    anything else is upstream's (502)."""
    detail = f"Plaid request failed: {error.code}"
    if error.code == "INVALID_PUBLIC_TOKEN":
        return ClientException(detail=detail)
    return ClientException(detail=detail, status_code=HTTP_502_BAD_GATEWAY)


async def _ledger_primary_currency(ledger: Ledger) -> str:
    """The ledger's primary currency is its owner's (PRD #31): the fallback
    must not depend on which member happened to click connect."""
    owner = await LedgerMember.where(
        lambda m: (m.ledger_id == ledger.id) & (m.role == LedgerRole.OWNER)
    ).first()
    user = (
        await User.where(lambda u, uid=owner.user_id: u.id == uid).first()  # ty: ignore[unresolved-attribute]
        if owner
        else None
    )
    if user is None:
        # Every ledger is created with an owner (M1 invariant); reaching
        # here means corrupted membership, not a request problem.
        raise RuntimeError(f"ledger {ledger.id} has no owner")
    return user.primary_currency


async def _get_connection(ledger: Ledger, connection_id: uuid.UUID) -> Connection:
    """Fetch within the acting ledger: another ledger's connection answers
    the same 404 as a nonexistent one — never a confirming 403."""
    connection = await Connection.where(
        lambda c: (c.id == connection_id) & (c.ledger_id == ledger.id)
    ).first()
    if connection is None:
        raise NotFoundException(detail="No such connection")
    return connection


async def _enqueue_sync(connection: Connection) -> None:
    """Defer one lock-serialized sync (ADR-0006: lock per connection)."""
    await sync_connection.configure(lock=f"sync:{connection.id}").defer_async(
        connection_id=str(connection.id)
    )


async def _connection_out(connection: Connection) -> ConnectionOut:
    cid = connection.id
    accounts = await Account.where(lambda a: a.connection_id == cid).all()
    return ConnectionOut(
        id=connection.id,
        provider=connection.provider,
        status=connection.status,
        last_synced_at=connection.last_synced_at,
        error_detail=connection.error_detail,
        accounts=[await account_out(a) for a in accounts],
        created_at=connection.created_at,
    )


@post("/link-token")
async def create_link_token(
    current_user: NamedDependency[User],
    current_ledger: NamedDependency[Ledger],
    data: LinkTokenIn | None = None,
) -> LinkTokenOut:
    """Shaped for a future frontend to drop Plaid Link on top unchanged;
    sandbox tests shortcut the widget between this and the exchange.
    With a ``connection_id`` this is the repair path: an update-mode token
    for the same Item."""
    _require_plaid()
    access_token: str | None = None
    if data is not None and data.connection_id is not None:
        connection = await _get_connection(current_ledger, data.connection_id)
        if connection.encrypted_secret is not None:
            access_token = decrypt_secret(connection.encrypted_secret)
    try:
        token = await providers.get_provider().create_link_token(
            client_user_id=str(current_user.id), access_token=access_token
        )
    except providers.ProviderError as error:
        raise _surface(error) from error
    return LinkTokenOut(link_token=token)


@post("/")
async def create_connection(
    data: ConnectionCreateIn,
    current_ledger: NamedDependency[Ledger],
) -> ConnectionOut:
    """Exchange the public token; create the Connection and one Account per
    account on the Item, atomically. Currency falls back to the ledger's
    primary currency when the provider omits it."""
    _require_plaid()
    provider = providers.get_provider()
    try:
        exchanged = await provider.exchange_public_token(data.public_token)
        provider_accounts = await provider.get_accounts(exchanged.access_token)
    except providers.ProviderError as error:
        raise _surface(error) from error
    fallback_currency = await _ledger_primary_currency(current_ledger)
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
                currency=pa.currency or fallback_currency,
                connection=connection,
                provider_account_id=pa.provider_account_id,
            )
    # Defer-after-commit: the initial sync is auto-enqueued — the story is
    # "accounts appear with balances", not "now call sync yourself" (PRD
    # #31); manual-only governs re-syncs.
    await _enqueue_sync(connection)
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
    return await _connection_out(await _get_connection(current_ledger, connection_id))


@post("/{connection_id:uuid}/sync", status_code=HTTP_202_ACCEPTED)
async def refresh_connection(
    connection_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Manual refresh (PRD #31): the one v0 re-sync trigger. 202 — the
    work happens in the worker; the health surface reports the outcome."""
    _require_plaid()
    connection = await _get_connection(current_ledger, connection_id)
    await _enqueue_sync(connection)


@delete("/{connection_id:uuid}")
async def delete_connection(
    connection_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Disconnect severs, never destroys (CONTEXT.md): Plaid's side is
    revoked, the connection row deleted, and the accounts live on with
    ``connection`` nulled (SET NULL) — structurally manual from here on.

    Ordering is deliberate: revoke first, sever second. If revocation
    fails transiently the client retries (502, nothing severed); Plaid
    already not knowing the item is success from this seat."""
    _require_plaid()
    connection = await _get_connection(current_ledger, connection_id)
    if connection.encrypted_secret is not None:
        try:
            await providers.get_provider().remove_item(decrypt_secret(connection.encrypted_secret))
        except providers.ProviderError as error:
            if error.code != "ITEM_NOT_FOUND":
                raise _surface(error) from error
    await Connection.where(lambda c, cid=connection.id: c.id == cid).delete()
    log.info(
        "connection.deleted",
        connection_id=str(connection.id),
        ledger_id=str(current_ledger.id),
    )


connections_router = Router(
    path="/api/v1/connections",
    route_handlers=[
        create_link_token,
        create_connection,
        list_connections,
        get_connection,
        refresh_connection,
        delete_connection,
    ],
)
