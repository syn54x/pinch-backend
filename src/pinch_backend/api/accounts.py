"""/api/v1/accounts — manual accounts and balance entries (PRD M4, issue #14).

The first domain endpoints: every handler reaches data via ``current_ledger``
(AGENTS I-2), every list returns ``Page[T]`` (M3, issue #9), every response
is an explicit allowlist, and tenancy misses answer 404 — never a
confirming 403. Writes are unsafe methods, so the M3 scope guard applies by
construction; no handler re-checks it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from datetime import date as CalendarDate
from typing import Annotated

from litestar import Router, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import FromPath, QueryParameter
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from pinch_backend import loans
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


class AccountPatchIn(BaseModel):
    """User-editable account fields: the label (M4) and, on loan/credit
    kinds, the loan terms (M8 CP4). Only fields present in the body are
    applied; present-and-null clears a term. A term on a kind that cannot
    carry it is a 400, never silence."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=100)
    apr: float | None = Field(default=None, ge=0, le=100)
    """A percentage: 4.9 means 4.9%."""
    minimum_payment_minor: StrictInt | None = Field(default=None, ge=0)
    origination_date: CalendarDate | None = None
    origination_amount_minor: StrictInt | None = Field(default=None, le=0)
    """Account-signed like every amount: the loan's opening balance,
    negative."""
    maturity_date: CalendarDate | None = None


TERM_FIELDS = frozenset(
    {
        "apr",
        "minimum_payment_minor",
        "origination_date",
        "origination_amount_minor",
        "maturity_date",
    }
)
_TERMS_BY_KIND: dict[AccountKind, frozenset[str]] = {
    AccountKind.LOAN: TERM_FIELDS,
    AccountKind.CREDIT: frozenset({"apr", "minimum_payment_minor"}),
}


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


class TermsOut(BaseModel):
    """The loan terms standing on a loan/credit account (M8 CP4)."""

    apr: float | None
    minimum_payment_minor: int | None
    origination_date: CalendarDate | None
    origination_amount_minor: int | None
    maturity_date: CalendarDate | None


class AccountOut(BaseModel):
    """What a client may see about an account — an allowlist, never the row."""

    id: uuid.UUID
    kind: AccountKind
    label: str
    currency: str
    mask: str | None
    manual: bool
    archived: bool
    balance: BalanceOut | None
    terms: TermsOut | None
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


def _terms_out(account: Account) -> TermsOut | None:
    """Null until any term is set — a checking account never grows a
    vestigial terms object."""
    values = {name: getattr(account, name) for name in TERM_FIELDS}
    if all(value is None for value in values.values()):
        return None
    return TermsOut(**values)


async def account_out(account: Account) -> AccountOut:
    """Public: the connections surface renders its accounts through this."""
    return AccountOut(
        id=account.id,
        kind=account.kind,
        label=account.label,
        currency=account.currency,
        mask=account.mask,
        manual=account.connection_id is None,  # ty: ignore[unresolved-attribute]
        archived=account.archived,
        balance=await _current_balance(account.id),
        terms=_terms_out(account),
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
    return await account_out(account)


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
    return Page(items=[await account_out(a) for a in rows], next_cursor=next_cursor)


@get("/{account_id:uuid}")
async def get_account(
    account_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> AccountOut:
    return await account_out(await _get_account(current_ledger, account_id))


@patch("/{account_id:uuid}")
async def update_account_label(
    account_id: FromPath[uuid.UUID],
    data: AccountPatchIn,
    current_ledger: NamedDependency[Ledger],
) -> AccountOut:
    """Label plus, on loan/credit kinds, the loan terms (M8 CP4). Kind and
    currency stay structural (transactions and entries bake them in)."""
    account = await _get_account(current_ledger, account_id)
    provided = data.model_fields_set
    provided_terms = provided & TERM_FIELDS
    allowed_terms = _TERMS_BY_KIND.get(account.kind, frozenset())
    forbidden = provided_terms - allowed_terms
    if forbidden:
        raise ClientException(
            detail=(f"{sorted(forbidden)[0]} does not apply to a {account.kind.value} account")
        )
    if data.label is not None:
        account.label = data.label
    for name in provided_terms:
        setattr(account, name, getattr(data, name))
    await account.save()
    log.info(
        "account.updated",
        account_id=str(account.id),
        ledger_id=str(current_ledger.id),
        fields=sorted(provided),
    )
    return await account_out(account)


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
    return await account_out(account)


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


class PayoffPoint(BaseModel):
    date: CalendarDate
    balance_minor: int


class SimulationOut(BaseModel):
    """One amortization run. ``never_pays_off`` true carries null months/
    date/interest and an empty curve — the honest shape, never fiction."""

    never_pays_off: bool
    months: int | None
    payoff_date: CalendarDate | None
    total_interest_minor: int | None
    series: list[PayoffPoint]


class PayoffHeadline(BaseModel):
    """Your behavior vs the contract minimum: the product's headline."""

    months_earlier: int
    interest_saved_minor: int


class PayoffProjections(BaseModel):
    at_pace: SimulationOut
    at_minimum: SimulationOut | None
    headline: PayoffHeadline | None


class PayoffScenario(BaseModel):
    """The what-if widget: the same pure simulator with extra_monthly
    added — stateless, nothing stored."""

    extra_monthly_minor: int
    months_sooner: int
    interest_saved_minor: int


class PayoffOut(BaseModel):
    account_id: uuid.UUID
    as_of: CalendarDate
    currency: str
    balance_minor: int | None
    apr: float | None
    minimum_payment_minor: int | None
    pace_payment_minor: int
    payoff_percent: float | None
    projections: PayoffProjections | None
    scenario: PayoffScenario | None


def _simulation_out(sim: loans.SimulatedPayoff) -> SimulationOut:
    return SimulationOut(
        never_pays_off=sim.never_pays_off,
        months=sim.months,
        payoff_date=sim.payoff_date,
        total_interest_minor=sim.total_interest_minor,
        series=[PayoffPoint(date=d, balance_minor=b) for d, b in sim.series],
    )


async def balance_at(account_id: uuid.UUID, as_of: CalendarDate) -> int | None:
    """Latest entry on or before ``as_of`` — the report-side, replayable
    sibling of _current_balance. None when nothing was ever observed."""
    cutoff = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC) + timedelta(days=1)
    entry = (
        await BalanceEntry.where(lambda b: (b.account_id == account_id) & (b.as_of < cutoff))
        .order_by(lambda b: b.as_of, "desc")
        .order_by(lambda b: b.id, "desc")
        .first()
    )
    return entry.amount_minor if entry is not None else None


def payoff_percent(account: Account, balance_minor: int | None) -> float | None:
    """The ring: share of the original principal paid down. Needs the
    origination amount and a balance; absent either, absent the ring."""
    if account.origination_amount_minor in (None, 0) or balance_minor is None:
        return None
    origination = abs(account.origination_amount_minor)
    return round((origination - abs(balance_minor)) / origination * 100, 4)


async def account_payoff(
    account: Account, as_of: CalendarDate, extra_monthly: int | None
) -> PayoffOut:
    """Public: the debt report renders its per-loan projections through
    the same derivation."""
    balance = await balance_at(account.id, as_of)
    pace = await loans.observed_pace(account.id, as_of)

    projections: PayoffProjections | None = None
    scenario: PayoffScenario | None = None
    if account.apr is not None and balance is not None:
        at_pace = loans.simulate_payoff(balance, account.apr, pace, as_of)
        at_minimum = (
            loans.simulate_payoff(balance, account.apr, account.minimum_payment_minor, as_of)
            if account.minimum_payment_minor is not None
            else None
        )
        headline = None
        if at_minimum is not None and not at_pace.never_pays_off and not at_minimum.never_pays_off:
            assert at_pace.months is not None and at_minimum.months is not None
            assert at_pace.total_interest_minor is not None
            assert at_minimum.total_interest_minor is not None
            headline = PayoffHeadline(
                months_earlier=at_minimum.months - at_pace.months,
                interest_saved_minor=(
                    at_minimum.total_interest_minor - at_pace.total_interest_minor
                ),
            )
        projections = PayoffProjections(
            at_pace=_simulation_out(at_pace),
            at_minimum=_simulation_out(at_minimum) if at_minimum is not None else None,
            headline=headline,
        )
        if extra_monthly is not None and not at_pace.never_pays_off:
            boosted = loans.simulate_payoff(balance, account.apr, pace + extra_monthly, as_of)
            assert at_pace.months is not None and boosted.months is not None
            assert at_pace.total_interest_minor is not None
            assert boosted.total_interest_minor is not None
            scenario = PayoffScenario(
                extra_monthly_minor=extra_monthly,
                months_sooner=at_pace.months - boosted.months,
                interest_saved_minor=(at_pace.total_interest_minor - boosted.total_interest_minor),
            )

    return PayoffOut(
        account_id=account.id,
        as_of=as_of,
        currency=account.currency,
        balance_minor=balance,
        apr=account.apr,
        minimum_payment_minor=account.minimum_payment_minor,
        pace_payment_minor=pace,
        payoff_percent=payoff_percent(account, balance),
        projections=projections,
        scenario=scenario,
    )


@get("/{account_id:uuid}/payoff")
async def get_payoff(
    account_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
    extra_monthly: Annotated[int | None, QueryParameter()] = None,
    as_of: Annotated[CalendarDate | None, QueryParameter()] = None,
) -> PayoffOut:
    """Payoff projections for one debt account: observed pace vs the
    contractual minimum, plus the stateless extra-payment scenario."""
    account = await _get_account(current_ledger, account_id)
    if account.kind not in (AccountKind.LOAN, AccountKind.CREDIT):
        raise ClientException(detail="payoff applies to loan and credit accounts")
    if extra_monthly is not None and extra_monthly <= 0:
        raise ClientException(detail="extra_monthly must be positive")
    return await account_payoff(
        account, as_of if as_of is not None else utcnow().date(), extra_monthly
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
        get_payoff,
    ],
)
