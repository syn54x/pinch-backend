"""/api/v1/reports — derived, read-only numbers, computed on read (PRD M8
issue #45; net worth is CP1 #47).

No snapshot tables and no jobs: balance data only changes on sync or hand
entry, so every figure derives from balance history at request time. Every
report takes ``as_of`` (default: today) — the clock seam for tests and an
honest replay surface for scripts — and answers in the ledger's primary
currency, surfacing foreign-currency balances as an explicit unconverted
remainder (the FX seam has no provider in v0; see pinch_backend.fx).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from litestar import Router, get
from litestar.di import NamedDependency
from litestar.exceptions import ClientException
from litestar.params import QueryParameter
from pydantic import BaseModel

from pinch_backend.api.connections import ledger_primary_currency
from pinch_backend.fx import get_rate
from pinch_backend.models import (
    Account,
    AccountKind,
    BalanceEntry,
    Category,
    Ledger,
    SplitLine,
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)

ASSET_KINDS = frozenset({AccountKind.DEPOSITORY, AccountKind.INVESTMENT, AccountKind.ASSET})
LIABILITY_KINDS = frozenset({AccountKind.CREDIT, AccountKind.LOAN})

RangeName = Literal["1m", "6m", "1y", "all"]

_RANGE_DAYS: dict[str, int | None] = {"1m": 30, "6m": 182, "1y": 365, "all": None}
_RANGE_STEP: dict[str, int] = {"1m": 1, "6m": 7, "1y": 7, "all": 30}
_RANGE_GRANULARITY: dict[str, str] = {
    "1m": "daily",
    "6m": "weekly",
    "1y": "weekly",
    "all": "monthly",
}


class SeriesPoint(BaseModel):
    date: date
    net_worth_minor: int


class AccountSeriesPoint(BaseModel):
    date: date
    balance_minor: int


class Delta(BaseModel):
    """A change against a reference value. ``percent`` is null when the
    reference is zero — never infinity."""

    delta_minor: int
    percent: float | None


class Projection(BaseModel):
    """The run-rate extrapolation: OLS over the observed range, horizon =
    range length. Deterministic math, keyless — "Penny's projection" is
    branding (PRD #45)."""

    series: list[SeriesPoint]
    endpoint: SeriesPoint


class AccountReportOut(BaseModel):
    id: uuid.UUID
    label: str
    kind: AccountKind
    currency: str
    balance_minor: int
    series: list[AccountSeriesPoint]


class ExcludedBalance(BaseModel):
    """The unconverted remainder: per-currency totals no rate exists for."""

    currency: str
    balance_minor: int


class NetWorthOut(BaseModel):
    as_of: date
    range: RangeName
    granularity: str
    currency: str
    net_worth_minor: int
    assets_minor: int
    liabilities_minor: int
    month_to_date: Delta
    since_range_start: Delta
    series: list[SeriesPoint]
    projection: Projection | None
    accounts: list[AccountReportOut]
    excluded: list[ExcludedBalance]


def _bucket_dates(as_of: date, start: date, step_days: int) -> list[date]:
    """Fixed-step buckets anchored at ``as_of`` walking back to ``start``
    (inclusive-ish: the first bucket is the last step landing >= start).
    The response carries the explicit dates, so the frontend never guesses."""
    dates = [as_of]
    while dates[-1] - timedelta(days=step_days) >= start:
        dates.append(dates[-1] - timedelta(days=step_days))
    return list(reversed(dates))


def _forward_fill(entries: list[tuple[datetime, int]], buckets: list[date]) -> list[int]:
    """Per-bucket last-known value; 0 before the first observation (the
    neutral element of the sum — an absent account contributes nothing)."""
    values: list[int] = []
    i = 0
    current = 0
    for bucket in buckets:
        cutoff = datetime(bucket.year, bucket.month, bucket.day, tzinfo=UTC) + timedelta(days=1)
        while i < len(entries) and entries[i][0] < cutoff:
            current = entries[i][1]
            i += 1
        values.append(current)
    return values


def _value_at(entries: list[tuple[datetime, int]], on: date) -> int:
    """Forward-filled value at one date (delta references)."""
    return _forward_fill(entries, [on])[0]


def _ols_projection(
    buckets: list[date],
    values: list[int],
    first_observation: date | None,
    as_of: date,
    horizon_days: int,
    step_days: int,
) -> Projection | None:
    """OLS over the observed portion of the series, extrapolated forward.
    Null when fewer than two buckets carry observations — a single point
    has no slope, and a fabricated flat line would lie."""
    if first_observation is None:
        return None
    points = [
        (bucket, value)
        for bucket, value in zip(buckets, values, strict=True)
        if bucket >= first_observation
    ]
    if len(points) < 2:
        return None
    xs = [(bucket - as_of).days for bucket, _ in points]
    ys = [float(value) for _, value in points]
    n = len(points)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / var_x
    intercept = mean_y - slope * mean_x
    series = []
    d = as_of + timedelta(days=step_days)
    end = as_of + timedelta(days=horizon_days)
    while d <= end:
        series.append(
            SeriesPoint(date=d, net_worth_minor=round(slope * (d - as_of).days + intercept))
        )
        d += timedelta(days=step_days)
    if not series:
        return None
    return Projection(series=series, endpoint=series[-1])


def _delta(now_value: int, reference: int) -> Delta:
    delta = now_value - reference
    percent = None if reference == 0 else round(delta / abs(reference) * 100, 4)
    return Delta(delta_minor=delta, percent=percent)


@get("/net-worth")
async def net_worth_report(
    current_ledger: NamedDependency[Ledger],
    range_: Annotated[RangeName, QueryParameter(query="range")] = "6m",
    as_of: Annotated[date | None, QueryParameter()] = None,
) -> NetWorthOut:
    """Net worth, its history, and the run-rate projection — computed on
    read by forward-filling each non-archived account's balance history
    (archived accounts are invisible here: the binding #33 hook)."""
    as_of = as_of if as_of is not None else utcnow().date()
    primary = await ledger_primary_currency(current_ledger)
    ledger_id = current_ledger.id
    accounts = await Account.where(
        lambda a: (a.ledger_id == ledger_id) & (a.archived == False)  # noqa: E712
    ).all()

    included: list[Account] = []
    excluded_accounts: list[Account] = []
    for account in accounts:
        if await get_rate(account.currency, primary, as_of) is not None:
            included.append(account)
        else:
            excluded_accounts.append(account)

    cutoff = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC) + timedelta(days=1)
    included_ids = [a.id for a in included]
    entries_by_account: dict[uuid.UUID, list[tuple[datetime, int]]] = {a.id: [] for a in included}
    if included_ids:
        rows = (
            await BalanceEntry.where(
                lambda b: (b.account_id.in_(included_ids)) & (b.as_of < cutoff)
            )
            .order_by(lambda b: b.as_of)
            .order_by(lambda b: b.id)
            .all()
        )
        for row in rows:
            entries_by_account[row.account_id].append((row.as_of, row.amount_minor))  # ty: ignore[unresolved-attribute]

    all_entries = [e for per_account in entries_by_account.values() for e in per_account]
    first_observation = min((e[0].date() for e in all_entries), default=None)

    range_days = _RANGE_DAYS[range_]
    step_days = _RANGE_STEP[range_]
    if range_days is not None:
        start = as_of - timedelta(days=range_days)
        horizon_days = range_days
    else:
        start = first_observation if first_observation is not None else as_of
        horizon_days = (as_of - start).days or step_days
    buckets = _bucket_dates(as_of, start, step_days)

    account_values = {
        account.id: _forward_fill(entries_by_account[account.id], buckets) for account in included
    }
    series_values = [
        sum(account_values[account.id][i] for account in included) for i in range(len(buckets))
    ]
    series = [
        SeriesPoint(date=bucket, net_worth_minor=value)
        for bucket, value in zip(buckets, series_values, strict=True)
    ]

    now_values = {account.id: account_values[account.id][-1] for account in included}
    assets = sum(v for a in included if a.kind in ASSET_KINDS for v in [now_values[a.id]])
    liabilities = sum(v for a in included if a.kind in LIABILITY_KINDS for v in [now_values[a.id]])
    net_worth = assets + liabilities

    month_start = as_of.replace(day=1) - timedelta(days=1)
    """Reference = the value standing when the month opened: forward-filled
    through the last day of the prior month."""
    month_reference = sum(_value_at(entries_by_account[a.id], month_start) for a in included)

    excluded_totals: dict[str, int] = {}
    for account in excluded_accounts:
        latest = (
            await BalanceEntry.where(
                lambda b, aid=account.id: (b.account_id == aid) & (b.as_of < cutoff)
            )
            .order_by(lambda b: b.as_of, "desc")
            .order_by(lambda b: b.id, "desc")
            .first()
        )
        if latest is not None:
            excluded_totals[account.currency] = (
                excluded_totals.get(account.currency, 0) + latest.amount_minor
            )

    return NetWorthOut(
        as_of=as_of,
        range=range_,
        granularity=_RANGE_GRANULARITY[range_],
        currency=primary,
        net_worth_minor=net_worth,
        assets_minor=assets,
        liabilities_minor=liabilities,
        month_to_date=_delta(net_worth, month_reference),
        since_range_start=_delta(net_worth, series_values[0]),
        series=series,
        projection=_ols_projection(
            buckets, series_values, first_observation, as_of, horizon_days, step_days
        ),
        accounts=[
            AccountReportOut(
                id=account.id,
                label=account.label,
                kind=account.kind,
                currency=account.currency,
                balance_minor=now_values[account.id],
                series=[
                    AccountSeriesPoint(date=bucket, balance_minor=value)
                    for bucket, value in zip(buckets, account_values[account.id], strict=True)
                ],
            )
            for account in included
        ],
        excluded=[
            ExcludedBalance(currency=currency, balance_minor=total)
            for currency, total in sorted(excluded_totals.items())
        ],
    )


class CategorySpendingRow(BaseModel):
    """One category's month, reported as positive magnitudes. ``rolled_up``
    includes descendants by ancestry (derived at read time, never stored);
    ``percent_change`` compares rolled-up against the previous month and is
    null when the previous month is zero. ``category_id``/``name`` null =
    the uncategorized bucket."""

    category_id: uuid.UUID | None
    name: str | None
    parent_id: uuid.UUID | None
    direct_minor: int
    rolled_up_minor: int
    previous_minor: int
    percent_change: float | None


class DaySpending(BaseModel):
    date: date
    total_minor: int


class PreviousMonth(BaseModel):
    month: str
    total_minor: int


class ExcludedSpending(BaseModel):
    currency: str
    total_minor: int


class SpendingOut(BaseModel):
    month: str
    currency: str
    total_minor: int
    by_day: list[DaySpending]
    by_category: list[CategorySpendingRow]
    previous: PreviousMonth
    change: Delta
    excluded: list[ExcludedSpending]


def _parse_month(month: str) -> date:
    try:
        parsed = datetime.strptime(month, "%Y-%m")
    except ValueError as error:
        raise ClientException(detail="month must be YYYY-MM") from error
    return date(parsed.year, parsed.month, 1)


def _next_month(month_start: date) -> date:
    return (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )


def _prev_month(month_start: date) -> date:
    return (
        date(month_start.year - 1, 12, 1)
        if month_start.month == 1
        else date(month_start.year, month_start.month - 1, 1)
    )


async def _spending_by_category(
    ledger_id: uuid.UUID, primary: str, start: date, end: date
) -> dict[uuid.UUID | None, int]:
    """Spending magnitudes per category for one window — the PRD's one
    definition: unsplit outflows under their own category plus outflow
    lines under theirs; transfers excluded by existence, the vacated split
    parent excluded by its lines' existence. Two grouped queries, merged."""
    unsplit = (
        await Transaction.select(lambda t: {"cat": t.category_id, "total": t.amount_minor.sum()})
        .where(
            lambda t: (
                (t.ledger_id == ledger_id)
                & (t.currency == primary)
                & (t.amount_minor < 0)
                & (t.date >= start)
                & (t.date < end)
                & ~t.transfer_out.exists()
                & ~t.transfer_in.exists()
                & ~t.split_lines.exists()
            )
        )
        .all()
    )
    lines = (
        await SplitLine.select(lambda ln: {"cat": ln.category_id, "total": ln.amount_minor.sum()})
        .where(
            lambda ln: (
                (ln.ledger_id == ledger_id)
                & (ln.amount_minor < 0)
                & (ln.transaction.currency == primary)
                & (ln.transaction.date >= start)
                & (ln.transaction.date < end)
            )
        )
        .all()
    )
    totals: dict[uuid.UUID | None, int] = {}
    for row in [*unsplit, *lines]:
        totals[row.cat] = totals.get(row.cat, 0) + -row.total  # ty: ignore[unresolved-attribute]
    return totals


async def _spending_by_day(
    ledger_id: uuid.UUID, primary: str, start: date, end: date
) -> dict[date, int]:
    """Daily grain straight from SQL (GROUP BY the date column — the CP0-
    verified backbone); sparse: only days with spending appear."""
    unsplit = (
        await Transaction.select(lambda t: {"d": t.date, "total": t.amount_minor.sum()})
        .where(
            lambda t: (
                (t.ledger_id == ledger_id)
                & (t.currency == primary)
                & (t.amount_minor < 0)
                & (t.date >= start)
                & (t.date < end)
                & ~t.transfer_out.exists()
                & ~t.transfer_in.exists()
                & ~t.split_lines.exists()
            )
        )
        .all()
    )
    lines = (
        await SplitLine.select(
            lambda ln: {"d": ln.transaction.date, "total": ln.amount_minor.sum()}
        )
        .where(
            lambda ln: (
                (ln.ledger_id == ledger_id)
                & (ln.amount_minor < 0)
                & (ln.transaction.currency == primary)
                & (ln.transaction.date >= start)
                & (ln.transaction.date < end)
            )
        )
        .all()
    )
    totals: dict[date, int] = {}
    for row in [*unsplit, *lines]:
        totals[row.d] = totals.get(row.d, 0) + -row.total  # ty: ignore[unresolved-attribute]
    return totals


def _rollup(
    direct: dict[uuid.UUID | None, int], categories: list[Category]
) -> dict[uuid.UUID | None, int]:
    """Rolled-up magnitudes: each category's direct plus its descendants',
    by walking the parent chain of every spent category. The uncategorized
    bucket has no ancestry — it rolls up to itself."""
    parents = {c.id: c.parent_id for c in categories}  # ty: ignore[unresolved-attribute]
    rolled: dict[uuid.UUID | None, int] = {}
    for category_id, amount in direct.items():
        if category_id is None:
            rolled[None] = rolled.get(None, 0) + amount
            continue
        node: uuid.UUID | None = category_id
        while node is not None:
            rolled[node] = rolled.get(node, 0) + amount
            node = parents.get(node)
    return rolled


@get("/spending")
async def spending_report(
    current_ledger: NamedDependency[Ledger],
    month: Annotated[str | None, QueryParameter()] = None,
    as_of: Annotated[date | None, QueryParameter()] = None,
) -> SpendingOut:
    """One month of spending — total, daily trend, by-category with rollup,
    and the period-over-period comparison against the prior month."""
    as_of = as_of if as_of is not None else utcnow().date()
    month_start = _parse_month(month) if month is not None else as_of.replace(day=1)
    month_end = _next_month(month_start)
    previous_start = _prev_month(month_start)
    primary = await ledger_primary_currency(current_ledger)
    ledger_id = current_ledger.id

    direct = await _spending_by_category(ledger_id, primary, month_start, month_end)
    previous_direct = await _spending_by_category(ledger_id, primary, previous_start, month_start)
    by_day = await _spending_by_day(ledger_id, primary, month_start, month_end)

    categories = await Category.where(lambda c: c.ledger_id == ledger_id).all()
    names = {c.id: c.name for c in categories}
    parent_ids = {c.id: c.parent_id for c in categories}  # ty: ignore[unresolved-attribute]
    rolled = _rollup(direct, categories)
    previous_rolled = _rollup(previous_direct, categories)

    rows: list[CategorySpendingRow] = []
    for category_id in sorted(
        set(rolled) | set(previous_rolled), key=lambda c: (c is None, names.get(c, ""))
    ):
        current_rolled = rolled.get(category_id, 0)
        prior = previous_rolled.get(category_id, 0)
        rows.append(
            CategorySpendingRow(
                category_id=category_id,
                name=names.get(category_id),
                parent_id=parent_ids.get(category_id),
                direct_minor=direct.get(category_id, 0),
                rolled_up_minor=current_rolled,
                previous_minor=prior,
                percent_change=(
                    None if prior == 0 else round((current_rolled - prior) / prior * 100, 4)
                ),
            )
        )

    total = sum(direct.values())
    previous_total = sum(previous_direct.values())

    excluded_rows = (
        await Transaction.select(lambda t: {"currency": t.currency, "total": t.amount_minor.sum()})
        .where(
            lambda t: (
                (t.ledger_id == ledger_id)
                & (t.currency != primary)
                & (t.amount_minor < 0)
                & (t.date >= month_start)
                & (t.date < month_end)
                & ~t.transfer_out.exists()
                & ~t.transfer_in.exists()
            )
        )
        .all()
    )

    return SpendingOut(
        month=month_start.strftime("%Y-%m"),
        currency=primary,
        total_minor=total,
        by_day=[DaySpending(date=d, total_minor=amount) for d, amount in sorted(by_day.items())],
        by_category=rows,
        previous=PreviousMonth(month=previous_start.strftime("%Y-%m"), total_minor=previous_total),
        change=_delta(total, previous_total),
        excluded=[
            ExcludedSpending(currency=row.currency, total_minor=-row.total)  # ty: ignore[unresolved-attribute]
            for row in sorted(excluded_rows, key=lambda r: r.currency)
        ],
    )


class DebtLoanRow(BaseModel):
    """One debt account's observation row. ``payoff_date`` is the at-pace
    projection — null when the loan lacks an APR/balance or never pays off
    at the observed pace."""

    id: uuid.UUID
    label: str
    kind: AccountKind
    apr: float | None
    balance_minor: int | None
    minimum_payment_minor: int | None
    pace_payment_minor: int
    payoff_percent: float | None
    payoff_date: date | None


class DebtOut(BaseModel):
    """The Debt screen's summary: total and count are always exact; every
    partial aggregate names how many loans it excluded — partial data
    annotates, never lies (PRD #45)."""

    as_of: date
    currency: str
    total_debt_minor: int
    loan_count: int
    monthly_minimums_minor: int
    minimums_excluded_count: int
    weighted_apr: float | None
    apr_excluded_count: int
    debt_free_by: date | None
    debt_free_excluded_count: int
    loans: list[DebtLoanRow]
    excluded: list[ExcludedBalance]


@get("/debt")
async def debt_report(
    current_ledger: NamedDependency[Ledger],
    as_of: Annotated[date | None, QueryParameter()] = None,
) -> DebtOut:
    """Every non-archived loan and credit account, observed and projected
    through the same derivation as GET /accounts/{id}/payoff."""
    from pinch_backend.api.accounts import account_payoff

    as_of = as_of if as_of is not None else utcnow().date()
    primary = await ledger_primary_currency(current_ledger)
    ledger_id = current_ledger.id
    debt_kinds = [AccountKind.LOAN, AccountKind.CREDIT]
    accounts = await Account.where(
        lambda a: (
            (a.ledger_id == ledger_id)
            & (a.archived == False)  # noqa: E712
            & (a.kind.in_(debt_kinds))
        )
    ).all()

    rows: list[DebtLoanRow] = []
    excluded_totals: dict[str, int] = {}
    total_debt = 0
    minimums = 0
    minimums_excluded = 0
    apr_weight_total = 0.0
    apr_balance_total = 0
    apr_excluded = 0
    payoff_dates: list[date] = []
    debt_free_excluded = 0
    for account in accounts:
        payoff = await account_payoff(account, as_of, None)
        if await get_rate(account.currency, primary, as_of) is None:
            if payoff.balance_minor is not None:
                excluded_totals[account.currency] = (
                    excluded_totals.get(account.currency, 0) + payoff.balance_minor
                )
            continue
        balance = payoff.balance_minor
        total_debt += balance or 0
        if account.minimum_payment_minor is not None:
            minimums += account.minimum_payment_minor
        else:
            minimums_excluded += 1
        if account.apr is not None and balance is not None:
            apr_weight_total += account.apr * abs(balance)
            apr_balance_total += abs(balance)
        else:
            apr_excluded += 1
        at_pace = payoff.projections.at_pace if payoff.projections is not None else None
        payoff_date = at_pace.payoff_date if at_pace is not None else None
        if payoff_date is not None:
            payoff_dates.append(payoff_date)
        else:
            debt_free_excluded += 1
        rows.append(
            DebtLoanRow(
                id=account.id,
                label=account.label,
                kind=account.kind,
                apr=account.apr,
                balance_minor=balance,
                minimum_payment_minor=account.minimum_payment_minor,
                pace_payment_minor=payoff.pace_payment_minor,
                payoff_percent=payoff.payoff_percent,
                payoff_date=payoff_date,
            )
        )

    return DebtOut(
        as_of=as_of,
        currency=primary,
        total_debt_minor=total_debt,
        loan_count=len(rows),
        monthly_minimums_minor=minimums,
        minimums_excluded_count=minimums_excluded,
        weighted_apr=(
            round(apr_weight_total / apr_balance_total, 4) if apr_balance_total else None
        ),
        apr_excluded_count=apr_excluded,
        debt_free_by=max(payoff_dates) if payoff_dates else None,
        debt_free_excluded_count=debt_free_excluded,
        loans=rows,
        excluded=[
            ExcludedBalance(currency=currency, balance_minor=total)
            for currency, total in sorted(excluded_totals.items())
        ],
    )


class UpcomingBill(BaseModel):
    display_name: str
    due_date: date
    amount_minor: int


class SubscriptionsCard(BaseModel):
    monthly_minor: int
    count: int


class BucketSlice(BaseModel):
    """One donut slice: recurring monthly outflow grouped by derived
    bucket (modal member category, Debt for loan-payment series, null for
    uncategorized)."""

    bucket: str | None
    monthly_minor: int


class CycleCard(BaseModel):
    paid: int
    total: int


class RecurringSummaryOut(BaseModel):
    """The Recurring screen's stat cards and donut, plus the Dashboard's
    next-7-days bills card — active, non-lapsed series only; income never
    counts into the outflow totals."""

    as_of: date
    monthly_recurring_minor: int
    due_next_7_days_minor: int
    due_next_7_days: list[UpcomingBill]
    subscriptions: SubscriptionsCard
    by_bucket: list[BucketSlice]
    cycle: CycleCard


@get("/recurring")
async def recurring_report(
    current_ledger: NamedDependency[Ledger],
    as_of: Annotated[date | None, QueryParameter()] = None,
) -> RecurringSummaryOut:
    from pinch_backend import recurring
    from pinch_backend.models import RecurringStatus

    as_of = as_of if as_of is not None else utcnow().date()
    ledger_id = current_ledger.id
    series_rows = await recurring.RecurringSeries.where(
        lambda s: (s.ledger_id == ledger_id) & (s.status == RecurringStatus.ACTIVE)
    ).all()
    names = await recurring.category_names_for(ledger_id)

    monthly_total = 0
    upcoming: list[UpcomingBill] = []
    subscriptions_total = 0
    subscriptions_count = 0
    buckets: dict[str | None, int] = {}
    paid = 0
    total = 0
    for series in series_rows:
        members = await recurring.series_members(series, as_of)
        state = recurring.cycle_state(series, members, as_of)
        if state.status == "lapsed":
            continue
        total += 1
        if state.status == "paid":
            paid += 1
        if series.direction > 0:
            continue
        monthly = state.monthly_minor or 0
        monthly_total += monthly
        if series.kind.value == "subscription":
            subscriptions_total += monthly
            subscriptions_count += 1
        bucket = await recurring.series_bucket(series, members, names)
        buckets[bucket] = buckets.get(bucket, 0) + monthly
        if (
            state.next_due_date is not None
            and state.due_in_days is not None
            and 0 <= state.due_in_days <= 7
        ):
            upcoming.append(
                UpcomingBill(
                    display_name=recurring.default_display_name(series),
                    due_date=state.next_due_date,
                    amount_minor=state.est_amount_minor or 0,
                )
            )

    return RecurringSummaryOut(
        as_of=as_of,
        monthly_recurring_minor=monthly_total,
        due_next_7_days_minor=sum(abs(u.amount_minor) for u in upcoming),
        due_next_7_days=sorted(upcoming, key=lambda u: u.due_date),
        subscriptions=SubscriptionsCard(
            monthly_minor=subscriptions_total, count=subscriptions_count
        ),
        by_bucket=[
            BucketSlice(bucket=bucket, monthly_minor=amount)
            for bucket, amount in sorted(buckets.items(), key=lambda item: -item[1])
        ],
        cycle=CycleCard(paid=paid, total=total),
    )


reports_router = Router(
    path="/api/v1/reports",
    route_handlers=[net_worth_report, spending_report, debt_report, recurring_report],
)
