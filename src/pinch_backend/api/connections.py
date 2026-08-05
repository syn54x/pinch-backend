"""/api/v1/connections — sync-provider connections (M7 CP1, issue #33;
provider-neutral sweep M13 CP1, #88; ADR 0009).

The connect flow is provider-neutral: a connect session (the opaque token
the provider's widget consumes) → the widget, or a sandbox shortcut →
completion, which creates the Connection and one Account per consented
provider account — no second selection layer; the widget already did
selection. Per-connection credentials (Plaid's access token) are
encrypted at rest and write-only at this surface; MX mints none —
``encrypted_secret`` is honestly NULL, and its binding is the ledger's
enrollment user plus the member guid (M13 CP2, ADR 0009).

Configuration is per provider: an instance refuses an unconfigured
provider's endpoints cleanly while everything else stands (PRD #86 story
16) — connected-account support never holds manual tracking hostage. The
catalog endpoint reports which providers this instance offers and what
each delivers (provider capabilities).
"""

import uuid
from datetime import datetime

from litestar import Router, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException, PermissionDeniedException
from litestar.params import FromPath
from litestar.status_codes import HTTP_202_ACCEPTED, HTTP_502_BAD_GATEWAY
from pydantic import BaseModel, ConfigDict

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
from pinch_backend.jobs import enqueue_sync_connection
from pinch_backend.models import (
    Account,
    BalanceEntry,
    BalanceSource,
    Connection,
    ConnectionProvider,
    ConnectionStatus,
    Enrollment,
    Ledger,
    LedgerMember,
    LedgerRole,
    User,
    transaction,
    utcnow,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class ConnectSessionIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    provider: ConnectionProvider
    """The sync provider the user chose — per connection, at connect time
    (CONTEXT.md: Sync provider)."""
    connection_id: uuid.UUID | None = None
    """Absent: a fresh connect. Present: a repair session for this
    connection's login (PRD #31 reauth) — no completion follows; the
    next successful sync is the healer."""


class ConnectSessionOut(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    provider: ConnectionProvider
    token: str
    """Opaque: whatever the provider's widget consumes (Plaid: a
    link_token; MX: a widget URL)."""


class ConnectionCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    provider: ConnectionProvider
    token: str
    """The provider's completion handle (Plaid: the public_token Link
    answered with) — verified server-side, never trusted naked."""


class ProviderCatalogEntry(BaseModel):
    """One known provider as this instance offers it (M13 CP1): the
    picker's honest coverage-and-capability surface (PRD #86 story 11)."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    provider: ConnectionProvider
    configured: bool
    """Whether this instance holds the provider's credentials — an
    unconfigured provider's endpoints refuse cleanly (story 16)."""
    capabilities: list[providers.ProviderCapability]
    """What the provider delivers (CONTEXT.md: Provider capability). A
    missing atom is a stated limit, never an error."""


class ConnectionOut(BaseModel):
    """What a client may see about a connection — an allowlist, never the
    row, and never the access token in any form."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    id: uuid.UUID
    provider: ConnectionProvider
    provider_institution_id: str | None
    """The provider's institution identity (M13 CP1) — the frontend dupe
    guard's basis for "you already connected this bank"."""
    institution_name: str | None
    status: ConnectionStatus
    last_synced_at: datetime | None
    error_detail: str | None
    investments_error_detail: str | None
    """The investments phase's independent health (M10 CP0): set while
    holdings can't be fetched, without the connection leaving ``active``."""
    investments_consent_required: bool
    """True when the Item predates investments consent (M10 CP2) — the
    frontend's cue to offer Enable investments (update-mode Link)."""
    accounts: list[AccountOut]
    created_at: datetime


REJECTED_TOKEN_CODES = frozenset({"INVALID_PUBLIC_TOKEN", "MEMBER_NOT_FOUND"})
"""The completion-handle rejections, one per provider: Plaid refusing a
stale public token, MX's member guid not living under our enrollment
(M13 CP2 — the never-trust-a-client-guid verdict). The client's fault,
never upstream's."""


def _require_provider(provider: ConnectionProvider) -> None:
    """The keyless stance, per provider since M13 CP1 (PRD #86 story 16):
    a clean refusal on the endpoints that would touch an unconfigured
    provider, and nothing else changes."""
    if not providers.provider_configured(provider):
        raise PermissionDeniedException(
            detail=f"{providers.PROVIDERS[provider].label} is not configured on this instance"
        )


def _surface(error: providers.ProviderError, provider: ConnectionProvider) -> Exception:
    """The recovery point for provider failures: the code — the only
    provider detail allowed out (PRD #31) — reaches the client instead of
    an opaque 500, naming the provider that failed. A rejected completion
    token is the client's fault (400); anything else is upstream's (502)."""
    detail = f"{providers.PROVIDERS[provider].label} request failed: {error.code}"
    if error.code in REJECTED_TOKEN_CODES:
        return ClientException(detail=detail)
    return ClientException(detail=detail, status_code=HTTP_502_BAD_GATEWAY)


async def ledger_primary_currency(ledger: Ledger) -> str:
    """The ledger's primary currency is its owner's (PRD #31): the fallback
    must not depend on which member happened to click connect."""
    owner = await LedgerMember.where(
        lambda m: (m.ledger_id == ledger.id) & (m.role == LedgerRole.OWNER)
    ).first()
    user = await User.where(lambda u, uid=owner.user_id: u.id == uid).first() if owner else None
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
    await enqueue_sync_connection(connection.id)


async def _mx_enrollment(ledger: Ledger) -> Enrollment | None:
    lid = ledger.id
    return await Enrollment.where(
        lambda e: (e.ledger_id == lid) & (e.provider == ConnectionProvider.MX)
    ).first()


async def _ensure_mx_enrollment(ledger: Ledger) -> Enrollment:
    """The ledger's MX user container (CONTEXT.md: Enrollment), created
    lazily at the first connect session and reused by every later one —
    exactly one row per (provider, ledger), the composite unique's law.
    A create racing itself can strand one unused MX-side user (the DB
    keeps one row; MX-side cleanup is not worth machinery for a
    two-clicks-at-once race)."""
    enrollment = await _mx_enrollment(ledger)
    if enrollment is None:
        user_guid = await providers.get_mx_provider().create_user(ledger_id=str(ledger.id))
        enrollment = await Enrollment.create(
            ledger=ledger, provider=ConnectionProvider.MX, provider_user_id=user_guid
        )
        log.info("enrollment.created", ledger_id=str(ledger.id), provider="mx")
    return enrollment


async def _bind_for_connect(
    provider: ConnectionProvider, ledger: Ledger, connection: Connection | None
) -> providers.SyncProvider:
    """Materialize the provider for a connect session, bound in its own
    credential shape (ADR 0009). Plaid: unbound for a fresh connect, the
    repair connection's decrypted access token for update mode. MX: the
    enrollment user (ensured lazily right here), plus the member guid
    when this is repair. May raise ``ProviderError`` (the MX enrollment
    ensure speaks to MX) — the caller surfaces it."""
    if provider is ConnectionProvider.MX:
        enrollment = await _ensure_mx_enrollment(ledger)
        return providers.get_provider(
            provider,
            user_guid=enrollment.provider_user_id,
            member_guid=None if connection is None else connection.provider_item_id,
        )
    if connection is None:
        return providers.get_provider(provider)
    if connection.encrypted_secret is None:
        # Plaid repair rides the stored token; a tokenless row must never
        # silently fall back to creation mode — a fresh connect on top of
        # a broken one duplicates accounts.
        raise ClientException(detail="Connection has no provider credentials to repair")
    return providers.get_provider(provider, secret=decrypt_secret(connection.encrypted_secret))


async def _connection_out(connection: Connection) -> ConnectionOut:
    cid = connection.id
    accounts = await Account.where(lambda a: a.connection_id == cid).all()
    return ConnectionOut(
        id=connection.id,
        provider=connection.provider,
        provider_institution_id=connection.provider_institution_id,
        institution_name=connection.institution_name,
        status=connection.status,
        last_synced_at=connection.last_synced_at,
        error_detail=connection.error_detail,
        investments_error_detail=connection.investments_error_detail,
        investments_consent_required=connection.investments_consent_required,
        accounts=[await account_out(a) for a in accounts],
        created_at=connection.created_at,
    )


@post("/connect-session")
async def create_connect_session(
    current_user: NamedDependency[User],
    current_ledger: NamedDependency[Ledger],
    data: ConnectSessionIn,
) -> ConnectSessionOut:
    """Mint the opaque token the chosen provider's widget consumes —
    shaped for the frontend to drop the widget on top unchanged; sandbox
    tests shortcut the widget between this and the completion. With a
    ``connection_id`` this is the repair path: a session bound to the
    same provider-side login."""
    _require_provider(data.provider)
    connection: Connection | None = None
    if data.connection_id is not None:
        connection = await _get_connection(current_ledger, data.connection_id)
        if connection.provider != data.provider:
            # A repair session is the connection's provider's to mint —
            # a mismatch is a client bug, never a silent re-route.
            raise ClientException(detail="Provider does not match the connection")
    try:
        provider = await _bind_for_connect(data.provider, current_ledger, connection)
        token = await provider.create_connect_session(client_user_id=str(current_user.id))
    except providers.ProviderError as error:
        raise _surface(error, data.provider) from error
    return ConnectSessionOut(provider=data.provider, token=token)


@get("/providers")
async def list_providers() -> list[ProviderCatalogEntry]:
    """The provider catalog (M13 CP1): one entry per known provider —
    configured or not, so the picker can render coverage and capability
    honestly instead of discovering refusals endpoint by endpoint."""
    return [
        ProviderCatalogEntry(
            provider=provider,
            configured=providers.provider_configured(provider),
            capabilities=list(providers.PROVIDERS[provider].capabilities),
        )
        for provider in ConnectionProvider
    ]


@post("/")
async def create_connection(
    data: ConnectionCreateIn,
    current_ledger: NamedDependency[Ledger],
) -> ConnectionOut:
    """Complete the connect: verify the token with the provider, then
    create the Connection and one Account per consented provider account,
    atomically. Currency falls back to the ledger's primary currency when
    the provider omits it. Balances the provider handed over in the same
    breath become the accounts' first balance entries (M13 CP2): MX
    aggregation finished before the widget answered, and its transaction
    sync — the balance writer — waits for CP3; Plaid's connect-time
    answer simply arrives a sync early. Either way the story is
    "accounts appear with balances", not "wait for a sync"."""
    _require_provider(data.provider)
    if data.provider is ConnectionProvider.MX:
        # The member guid is only verifiable under this ledger's own
        # enrollment; no enrollment means no session was ever minted
        # here, so the token cannot be ours — a client bug, 400.
        enrollment = await _mx_enrollment(current_ledger)
        if enrollment is None:
            raise ClientException(detail="No MX enrollment for this ledger")
        provider = providers.get_provider(data.provider, user_guid=enrollment.provider_user_id)
    else:
        provider = providers.get_provider(data.provider)
    try:
        result = await provider.complete_connect(data.token)
        provider_accounts = await provider.get_accounts()
    except providers.ProviderError as error:
        raise _surface(error, data.provider) from error
    fallback_currency = await ledger_primary_currency(current_ledger)
    observed_at = utcnow()
    async with transaction():
        connection = await Connection.create(
            ledger=current_ledger,
            provider=data.provider,
            provider_item_id=result.provider_item_id,
            provider_institution_id=result.provider_institution_id,
            institution_name=result.institution_name,
            status=ConnectionStatus.ACTIVE,
            # Honestly NULL for providers that mint no per-connection
            # secret (ADR 0009) — Plaid always answers one.
            encrypted_secret=(
                None if result.secret is None else encrypt_secret(result.secret.get_secret_value())
            ),
        )
        for pa in provider_accounts:
            account = await Account.create(
                ledger=current_ledger,
                kind=pa.kind,
                label=pa.name,
                currency=pa.currency or fallback_currency,
                connection=connection,
                provider_account_id=pa.provider_account_id,
                mask=pa.mask,
            )
            if pa.balance_minor is not None:
                await BalanceEntry.create(
                    ledger=current_ledger,
                    account=account,
                    amount_minor=pa.balance_minor,
                    currency=account.currency,
                    as_of=observed_at,
                    source=BalanceSource.PROVIDER,
                )
    # Defer-after-commit: the initial sync is auto-enqueued — the story is
    # "accounts appear with balances", not "now call sync yourself" (PRD
    # #31); manual-only governs re-syncs.
    await _enqueue_sync(connection)
    log.info(
        "connection.created",
        connection_id=str(connection.id),
        ledger_id=str(connection.ledger_id),
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
    work happens in the worker; the health surface reports the outcome.
    The refusal is per the connection's own provider (M13 CP1): a
    keyless instance answers 404 for connections it can't hold anyway."""
    connection = await _get_connection(current_ledger, connection_id)
    _require_provider(connection.provider)
    await _enqueue_sync(connection)


@delete("/{connection_id:uuid}")
async def delete_connection(
    connection_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    """Disconnect severs, never destroys (CONTEXT.md): the provider's
    side is revoked (Plaid's Item removed, MX's member deleted), the
    connection row deleted, and the accounts live on with ``connection``
    nulled (SET NULL) — structurally manual from here on. The MX
    enrollment persists: it is the ledger's container, reused by the
    next connect (M13 CP2).

    Ordering is deliberate: revoke first, sever second. If revocation
    fails transiently the client retries (502, nothing severed); the
    provider already not knowing the connection is success from this
    seat."""
    connection = await _get_connection(current_ledger, connection_id)
    _require_provider(connection.provider)
    provider: providers.SyncProvider | None = None
    if connection.provider is ConnectionProvider.MX:
        enrollment = await _mx_enrollment(current_ledger)
        # An MX connection without its enrollment is corrupted state, not
        # a request problem: connects only ever ride an ensured enrollment.
        if enrollment is None:
            raise RuntimeError(f"MX connection {connection.id} has no enrollment")
        provider = providers.get_provider(
            connection.provider,
            user_guid=enrollment.provider_user_id,
            member_guid=connection.provider_item_id,
        )
    elif connection.encrypted_secret is not None:
        provider = providers.get_provider(
            connection.provider, secret=decrypt_secret(connection.encrypted_secret)
        )
    if provider is not None:
        try:
            await provider.remove_item()
        except providers.ProviderError as error:
            if error.code not in providers.ALREADY_DISCONNECTED_CODES:
                raise _surface(error, connection.provider) from error
    await Connection.where(lambda c, cid=connection.id: c.id == cid).delete()
    log.info(
        "connection.deleted",
        connection_id=str(connection.id),
        ledger_id=str(current_ledger.id),
    )


connections_router = Router(
    path="/api/v1/connections",
    route_handlers=[
        create_connect_session,
        list_providers,
        create_connection,
        list_connections,
        get_connection,
        refresh_connection,
        delete_connection,
    ],
)
