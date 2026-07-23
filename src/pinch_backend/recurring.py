"""The recurring engine (PRD M8 #45, CP3 #49; CONTEXT.md: Recurring series,
Cadence, Cycle).

Detection is a post-classification pass in classify_ledger — sync, import
commit, and manual entry all funnel through it, so onboarding's initial
sync detects for free. Two-pass cadence fitting over a full-ledger scan
(the M7 detector's accepted v0 posture): pass 1 fits merged payee groups
(catches variable amounts and price hikes); pass 2 sub-groups pass-1
failures by exact amount (catches aggregator payees — the Apple case).
Per-cadence consistency guards make silence deterministic on coincidence
patterns: a wrong series is worse than a missing one.

Cycle state computes on read in a calendar-month frame (the wireframe's
"This cycle"): paid = a member this month; due/overdue against the next
expected date; lapsed = no member for two cadences (the data's verdict,
self-reversing); dismissed = the user's verdict, permanent.
"""

import itertools
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid

from pinch_backend.loans import add_months
from pinch_backend.models import (
    Account,
    AccountKind,
    Category,
    RecurringCadence,
    RecurringKind,
    RecurringSeries,
    RecurringStatus,
    Transaction,
    Transfer,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)

MIN_OCCURRENCES = 3
RECENT_AMOUNTS = 6
"""Estimates read the last six member amounts."""

_INTERVAL_BOUNDS: dict[RecurringCadence, tuple[int, int]] = {
    RecurringCadence.WEEKLY: (6, 8),
    RecurringCadence.BIWEEKLY: (11, 17),
    RecurringCadence.MONTHLY: (27, 34),
    RecurringCadence.QUARTERLY: (83, 99),
    RecurringCadence.YEARLY: (350, 380),
}
_WEEKDAY_CADENCES = frozenset({RecurringCadence.WEEKLY, RecurringCadence.BIWEEKLY})
_MONTHLY_FACTOR: dict[RecurringCadence, float] = {
    RecurringCadence.WEEKLY: 52 / 12,
    RecurringCadence.BIWEEKLY: 26 / 12,
    RecurringCadence.MONTHLY: 1.0,
    RecurringCadence.QUARTERLY: 1 / 3,
    RecurringCadence.YEARLY: 1 / 12,
}
_DOM_SPREAD_MAX = 3
"""Day-of-month consistency for monthly+ cadences; days >= 28 read as one
month-end equivalence class."""


def _advance(day: date, cadence: RecurringCadence, periods: int = 1) -> date:
    if cadence is RecurringCadence.WEEKLY:
        return day + timedelta(days=7 * periods)
    if cadence is RecurringCadence.BIWEEKLY:
        return day + timedelta(days=14 * periods)
    months = {
        RecurringCadence.MONTHLY: 1,
        RecurringCadence.QUARTERLY: 3,
        RecurringCadence.YEARLY: 12,
    }[cadence]
    return add_months(day, months * periods)


def fit_cadence(dates: list[date]) -> RecurringCadence | None:
    """Fit distinct, sorted occurrence dates against the cadence menu.

    Every consecutive interval must sit inside the cadence's bounds, and
    the cadence's consistency invariant must hold: weekly/biweekly charges
    land on one weekday (payroll does; two interleaved monthlies drift);
    monthly+ charges keep one day-of-month (±3, month-end clamped). Fails
    → None: silence, never a misfit."""
    if len(dates) < MIN_OCCURRENCES:
        return None
    intervals = [(b - a).days for a, b in itertools.pairwise(dates)]
    for cadence, (low, high) in _INTERVAL_BOUNDS.items():
        if not all(low <= interval <= high for interval in intervals):
            continue
        if cadence in _WEEKDAY_CADENCES:
            if len({d.weekday() for d in dates}) == 1:
                return cadence
            continue
        doms = [min(d.day, 28) for d in dates]
        if max(doms) - min(doms) <= _DOM_SPREAD_MAX:
            return cadence
    return None


@dataclass(frozen=True)
class _Occurrence:
    date: date
    amount_minor: int


def _fit_group(occurrences: list[_Occurrence]) -> RecurringCadence | None:
    dates = sorted({o.date for o in occurrences})
    return fit_cadence(dates)


async def detect_recurring(ledger_id: uuid.UUID) -> None:
    """The idempotent per-ledger sweep. Creates missing series, updates a
    drifted cadence, and never touches kind, display_name, or a dismissal
    — curation and verdicts are not detection's to overwrite."""
    accounts = await Account.where(lambda a: a.ledger_id == ledger_id).all()
    kinds = {a.id: a.kind for a in accounts}
    transactions = await Transaction.where(
        lambda t: (t.ledger_id == ledger_id) & (t.amount_minor != 0)
    ).all()

    groups: dict[tuple[uuid.UUID, str, int], list[_Occurrence]] = {}
    for txn in transactions:
        direction = 1 if txn.amount_minor > 0 else -1
        if direction > 0 and kinds.get(txn.account_id) in (  # ty: ignore[unresolved-attribute]
            AccountKind.LOAN,
            AccountKind.CREDIT,
        ):
            # An inflow on a debt account is a payment received — the
            # counterpart of a tracked outflow, never income to detect.
            continue
        key = (txn.account_id, txn.description_normalized, direction)  # ty: ignore[unresolved-attribute]
        groups.setdefault(key, []).append(_Occurrence(txn.date, txn.amount_minor))

    existing = {
        (s.account_id, s.payee, s.direction, s.amount_minor): s  # ty: ignore[unresolved-attribute]
        for s in await RecurringSeries.where(lambda s: s.ledger_id == ledger_id).all()
    }

    async def upsert(
        account_id: uuid.UUID,
        payee: str,
        direction: int,
        amount_minor: int | None,
        cadence: RecurringCadence,
    ) -> None:
        series = existing.get((account_id, payee, direction, amount_minor))
        if series is not None:
            if series.status is RecurringStatus.ACTIVE and series.cadence != cadence:
                series.cadence = cadence
                await series.save()
            return
        created = await RecurringSeries.create(
            ledger_id=ledger_id,
            account_id=account_id,
            payee=payee,
            direction=direction,
            amount_minor=amount_minor,
            cadence=cadence,
            kind=RecurringKind.INCOME if direction > 0 else RecurringKind.BILL,
        )
        existing[(account_id, payee, direction, amount_minor)] = created
        log.info(
            "recurring.detected",
            series_id=str(created.id),
            ledger_id=str(ledger_id),
            payee=payee,
            cadence=cadence.value,
        )

    for (account_id, payee, direction), occurrences in groups.items():
        cadence = _fit_group(occurrences)
        if cadence is not None:
            await upsert(account_id, payee, direction, None, cadence)
            continue
        by_amount: dict[int, list[_Occurrence]] = {}
        for occurrence in occurrences:
            by_amount.setdefault(occurrence.amount_minor, []).append(occurrence)
        if len(by_amount) < 2:
            continue
        for amount, sub in by_amount.items():
            sub_cadence = _fit_group(sub)
            if sub_cadence is not None:
                await upsert(account_id, payee, direction, amount, sub_cadence)


@dataclass(frozen=True)
class CycleState:
    """One series' current cycle, computed at read time."""

    status: str
    """paid | due | overdue | upcoming | lapsed"""
    last_paid_date: date | None
    next_due_date: date | None
    due_in_days: int | None
    fixed: bool
    est_amount_minor: int | None
    monthly_minor: int | None
    """The series' contribution to monthly totals (magnitude, cadence-
    normalized); None when there is nothing to estimate."""


async def series_members(series: RecurringSeries, as_of: date) -> list[Transaction]:
    """Match-on-read, newest first, bounded: the matcher is the query."""
    account_id = series.account_id  # ty: ignore[unresolved-attribute]
    payee = series.payee
    amount = series.amount_minor
    query = Transaction.where(
        lambda t: (
            (t.account_id == account_id) & (t.description_normalized == payee) & (t.date <= as_of)
        )
    )
    if series.direction > 0:
        query = query.where(lambda t: t.amount_minor > 0)
    else:
        query = query.where(lambda t: t.amount_minor < 0)
    if amount is not None:
        query = query.where(lambda t: t.amount_minor == amount)
    return (
        await query.order_by(lambda t: t.date, "desc")
        .order_by(lambda t: t.id, "desc")
        .limit(RECENT_AMOUNTS * 4)
        .all()
    )


def cycle_state(series: RecurringSeries, members: list[Transaction], as_of: date) -> CycleState:
    if not members:
        return CycleState("lapsed", None, None, None, False, None, None)
    last = members[0].date
    next_due = _advance(last, series.cadence)
    due_in = (next_due - as_of).days
    recents = [m.amount_minor for m in members[:RECENT_AMOUNTS]]
    fixed = max(recents) - min(recents) <= abs(int(statistics.median(recents))) // 100
    est = recents[0] if fixed else int(statistics.median(recents))
    monthly = round(abs(est) * _MONTHLY_FACTOR[series.cadence])

    if as_of >= _advance(last, series.cadence, 2):
        status = "lapsed"
    elif (last.year, last.month) == (as_of.year, as_of.month):
        status = "paid"
    elif due_in < 0:
        status = "overdue"
    elif (next_due.year, next_due.month) == (as_of.year, as_of.month):
        status = "due"
    else:
        status = "upcoming"
    return CycleState(status, last, next_due, due_in, fixed, est, monthly)


async def series_bucket(
    series: RecurringSeries, members: list[Transaction], category_names: dict[uuid.UUID, str]
) -> str | None:
    """The donut's grouping: Debt when members are transfer legs whose
    counterpart is a loan/credit account; otherwise the modal member
    category; otherwise None (uncategorized)."""
    member_ids = [m.id for m in members]
    if member_ids:
        transfers = await Transfer.where(
            lambda tr: (
                tr.outflow_transaction_id.in_(member_ids) | tr.inflow_transaction_id.in_(member_ids)
            )
        ).all()
        counterpart_ids = [
            other
            for tr in transfers
            for other in (tr.outflow_transaction_id, tr.inflow_transaction_id)  # ty: ignore[unresolved-attribute]
            if other is not None and other not in member_ids
        ]
        if counterpart_ids:
            debt_kinds = [AccountKind.LOAN, AccountKind.CREDIT]
            counterparts = await Transaction.where(
                lambda t: t.id.in_(counterpart_ids) & t.account.kind.in_(debt_kinds)
            ).all()
            if counterparts:
                return "Debt"
    counts: dict[uuid.UUID, int] = {}
    for member in members:
        if member.category_id is not None:  # ty: ignore[unresolved-attribute]
            counts[member.category_id] = counts.get(member.category_id, 0) + 1  # ty: ignore[unresolved-attribute]
    if not counts:
        return None
    modal = max(counts.items(), key=lambda item: item[1])[0]
    return category_names.get(modal)


async def category_names_for(ledger_id: uuid.UUID) -> dict[uuid.UUID, str]:
    return {c.id: c.name for c in await Category.where(lambda c: c.ledger_id == ledger_id).all()}


def default_display_name(series: RecurringSeries) -> str:
    """Amount-scoped series disambiguate: three series named "apple.com"
    are useless."""
    if series.display_name is not None:
        return series.display_name
    if series.amount_minor is None:
        return series.payee
    return f"{series.payee} · {abs(series.amount_minor) / 100:.2f}"
