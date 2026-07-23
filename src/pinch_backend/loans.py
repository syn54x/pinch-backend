"""Loan observation and payoff simulation (PRD M8 #45, CP4 #50).

Pace is *observed* behavior: the median of the trailing six calendar
months' payment totals into the loan, where a payment is a transfer whose
inflow side is the loan account (the M7 hook). The simulator is plain
amortization — monthly compounding at ``apr/12``, iterate to zero — run
once at pace and once at the contractual minimum; the difference is the
headline. "Never pays off at this pace" is a legitimate answer, returned
plainly, never extrapolated into fiction.
"""

import statistics
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid

from pinch_backend.models import Transaction

PACE_WINDOW_MONTHS = 6
_MAX_MONTHS = 1200
"""Simulation ceiling (a century): anything longer reads as never — the
honest answer for a payment barely above the interest."""


def add_months(day: date, months: int) -> date:
    """Calendar-month addition, clamping the day into the target month
    (Jan 31 + 1 month = Feb 28)."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - date.resolution).day
    return date(year, month, min(day.day, last_day))


@dataclass(frozen=True)
class SimulatedPayoff:
    """One amortization run. ``never_pays_off`` carries empty projections —
    months/date/interest are None and the curve is empty."""

    never_pays_off: bool
    months: int | None
    payoff_date: date | None
    total_interest_minor: int | None
    series: list[tuple[date, int]]
    """Monthly remaining balances, account-signed (negative), ending at 0."""


async def observed_pace(account_id: "uuid.UUID", as_of: date) -> int:
    """Median of the trailing six calendar months' monthly payment totals
    (the months *before* the as_of month — a partial month would understate
    the habit). Months without a payment count as zero."""
    window_end = as_of.replace(day=1)
    window_start = add_months(window_end, -PACE_WINDOW_MONTHS)
    payments = await Transaction.where(
        lambda t: (
            (t.account_id == account_id)
            & (t.amount_minor > 0)
            & (t.date >= window_start)
            & (t.date < window_end)
            & t.transfer_in.exists()
        )
    ).all()
    totals: dict[tuple[int, int], int] = {
        (add_months(window_start, k).year, add_months(window_start, k).month): 0
        for k in range(PACE_WINDOW_MONTHS)
    }
    for payment in payments:
        totals[(payment.date.year, payment.date.month)] += payment.amount_minor
    return int(statistics.median(totals.values()))


def simulate_payoff(
    balance_minor: int, apr: float, payment_minor: int, as_of: date
) -> SimulatedPayoff:
    """Amortize ``balance_minor`` (negative, account-signed) at ``apr``
    percent with a fixed monthly ``payment_minor`` until zero."""
    debt = -balance_minor
    if debt <= 0:
        return SimulatedPayoff(False, 0, as_of, 0, [])
    monthly_rate = apr / 100 / 12
    total_interest = 0
    series: list[tuple[date, int]] = []
    months = 0
    while debt > 0:
        interest = round(debt * monthly_rate)
        if payment_minor <= interest or months >= _MAX_MONTHS:
            return SimulatedPayoff(True, None, None, None, [])
        months += 1
        total_interest += interest
        debt = max(0, debt + interest - payment_minor)
        series.append((add_months(as_of, months), -debt))
    return SimulatedPayoff(False, months, add_months(as_of, months), total_interest, series)
