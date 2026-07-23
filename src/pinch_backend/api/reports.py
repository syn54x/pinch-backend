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
from litestar.params import QueryParameter
from pydantic import BaseModel

from pinch_backend.api.connections import ledger_primary_currency
from pinch_backend.fx import get_rate
from pinch_backend.models import Account, AccountKind, BalanceEntry, Ledger, utcnow
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


reports_router = Router(path="/api/v1/reports", route_handlers=[net_worth_report])
