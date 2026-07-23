"""FX seam (PRD M8 #45, CP1 #47): rate resolution for cross-currency reports.

v0 ships the seam without a provider: :func:`get_rate` answers ``None`` for
every cross-currency pair, and reports exclude unconverted balances
explicitly rather than summing a fake rate — the PRD's recorded deviation
from the epic's "current-rate approximation". A rates provider later slots
in behind this function with no API-shape change; same internal-interface
stance as valuation providers.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

__all__ = ["get_rate"]


async def get_rate(from_currency: str, to_currency: str, on: "date") -> Decimal | None:
    """The rate multiplying a ``from_currency`` amount into ``to_currency``
    as observed on ``on`` — or ``None`` when no rate is known.

    Same-currency is always 1; everything else is unknown in v0.
    """
    if from_currency == to_currency:
        return Decimal(1)
    return None
