"""/api/v1/accounts — manual accounts and balance entries (PRD M4, issue #14).

The first domain endpoints: every handler reaches data via ``current_ledger``
(AGENTS I-2), every list returns ``Page[T]`` (M3, issue #9), every response
is an explicit allowlist, and tenancy misses answer 404 — never a
confirming 403. Writes are unsafe methods, so the M3 scope guard applies by
construction; no handler re-checks it.
"""

import uuid
from datetime import datetime

from litestar import Router, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import (
    Account,
    AccountKind,
    BalanceEntry,
    BalanceSource,
    Ledger,
    utcnow,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class AccountCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    kind: AccountKind
    label: str = Field(min_length=1, max_length=100)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    """Explicit, always: money never travels without ISO 4217 (CONTEXT.md)."""


class AccountLabelIn(BaseModel):
    label: str = Field(min_length=1, max_length=100)


class BalanceEntryCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    amount_minor: StrictInt
    """Integer minor units; a fractional amount is invalid, never rounded
    (I-1). Signed: loans and credit carry negative balances."""
    as_of: datetime | None = None
    """When the balance was observed; defaults to now. Backdating is how
    history gets backfilled."""


class BalanceOut(BaseModel):
    """An account's current balance: its latest entry by ``as_of``."""

    amount_minor: int
    currency: str
    as_of: datetime


class AccountOut(BaseModel):
    """What a client may see about an account — an allowlist, never the row."""

    id: uuid.UUID
    kind: AccountKind
    label: str
    currency: str
    manual: bool
    archived: bool
    balance: BalanceOut | None
    created_at: datetime


class BalanceEntryOut(BaseModel):
    """One row of balance history — an allowlist, never the row."""

    id: uuid.UUID
    amount_minor: int
    currency: str
    as_of: datetime
    source: BalanceSource
    created_at: datetime


async def _current_balance(account_id: uuid.UUID) -> BalanceOut | None:
    """Latest entry by ``as_of`` (id-descending tiebreak: of two entries for
    the same instant, the later-created one wins). None when no entries
    exist — never a fake zero."""
    entry = (
        await BalanceEntry.where(lambda b: b.account_id == account_id)
        .order_by(lambda b: b.as_of, "desc")
        .order_by(lambda b: b.id, "desc")
        .first()
    )
    if entry is None:
        return None
    return BalanceOut(amount_minor=entry.amount_minor, currency=entry.currency, as_of=entry.as_of)


async def _account_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        kind=account.kind,
        label=account.label,
        currency=account.currency,
        manual=account.connection_id is None,  # ty: ignore[unresolved-attribute]
        archived=account.archived,
        balance=await _current_balance(account.id),
        created_at=account.created_at,
    )


async def _get_account(ledger: Ledger, account_id: uuid.UUID) -> Account:
    """Fetch within the acting ledger: another ledger's account answers the
    same 404 as a nonexistent one — never a confirming 403."""
    account = await Account.where(
        lambda a: (a.id == account_id) & (a.ledger_id == ledger.id)
    ).first()
    if account is None:
        raise NotFoundException(detail="No such account")
    return account


@post("/")
async def create_account(
    data: AccountCreateIn, current_ledger: NamedDependency[Ledger]
) -> AccountOut:
    """A manual account: no connection, by construction (CONTEXT.md)."""
    account = await Account.create(
        ledger=current_ledger, kind=data.kind, label=data.label, currency=data.currency
    )
    log.info(
        "account.created",
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
        kind=data.kind.value,
    )
    return await _account_out(account)


@get("/")
async def list_accounts(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[AccountOut]:
    """Archived accounts stay listed (story 3): closed is a state, not an
    exit. Balances are one indexed lookup per row, bounded by the page cap —
    the single-statement form is an M8 concern, arriving with derivation."""
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        Account.where(lambda a: a.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(items=[await _account_out(a) for a in rows], next_cursor=next_cursor)


@get("/{account_id:uuid}")
async def get_account(
    account_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> AccountOut:
    return await _account_out(await _get_account(current_ledger, account_id))


@patch("/{account_id:uuid}")
async def update_account_label(
    account_id: FromPath[uuid.UUID],
    data: AccountLabelIn,
    current_ledger: NamedDependency[Ledger],
) -> AccountOut:
    """The label is the only user-editable field in M4; kind and currency
    are structural (transactions and entries bake them in)."""
    account = await _get_account(current_ledger, account_id)
    account.label = data.label
    await account.save()
    log.info(
        "account.label_updated",
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
    )
    return await _account_out(account)


@post("/{account_id:uuid}/archive", status_code=HTTP_200_OK)
async def archive_account(
    account_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> AccountOut:
    """A flag flip, idempotently: "archived" is the outcome, so repeating
    the request repeats the answer. Delete does not exist (story 3)."""
    account = await _get_account(current_ledger, account_id)
    if not account.archived:
        account.archived = True
        await account.save()
        log.info(
            "account.archived",
            account_id=str(account.id),
            ledger_id=str(current_ledger.id),
        )
    return await _account_out(account)


@post("/{account_id:uuid}/balance-entries")
async def create_balance_entry(
    account_id: FromPath[uuid.UUID],
    data: BalanceEntryCreateIn,
    current_ledger: NamedDependency[Ledger],
) -> BalanceEntryOut:
    """Hand-entered, so source=manual and the currency is the account's —
    a balance observation can't disagree with the account it observes."""
    account = await _get_account(current_ledger, account_id)
    entry = await BalanceEntry.create(
        ledger=current_ledger,
        account=account,
        amount_minor=data.amount_minor,
        currency=account.currency,
        as_of=data.as_of if data.as_of is not None else utcnow(),
        source=BalanceSource.MANUAL,
    )
    log.info(
        "balance_entry.created",
        entry_id=str(entry.id),
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
    )
    return BalanceEntryOut(
        id=entry.id,
        amount_minor=entry.amount_minor,
        currency=entry.currency,
        as_of=entry.as_of,
        source=entry.source,
        created_at=entry.created_at,
    )


@get("/{account_id:uuid}/balance-entries")
async def list_balance_entries(
    account_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[BalanceEntryOut]:
    """Balance history (story 2): every entry is retained; M8 charts it."""
    account = await _get_account(current_ledger, account_id)
    aid = account.id
    rows, next_cursor = await paginate(
        BalanceEntry.where(lambda b: b.account_id == aid), cursor=cursor, limit=limit
    )
    return Page(
        items=[
            BalanceEntryOut(
                id=b.id,
                amount_minor=b.amount_minor,
                currency=b.currency,
                as_of=b.as_of,
                source=b.source,
                created_at=b.created_at,
            )
            for b in rows
        ],
        next_cursor=next_cursor,
    )


accounts_router = Router(
    path="/api/v1/accounts",
    route_handlers=[
        create_account,
        list_accounts,
        get_account,
        update_account_label,
        archive_account,
        create_balance_entry,
        list_balance_entries,
    ],
)
