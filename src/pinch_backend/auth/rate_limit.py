"""Sliding-window rate limiting over database rows (PRD M2 story 4).

One INSERT per guarded attempt, one COUNT per check — atomic by
construction, no lost-update races a read-modify-write counter would have,
and no infrastructure beyond the one datastore (ADR-0003). Auth-endpoint
volume is tiny; a B-tree on ``key`` covers this for years.
"""

from typing import TYPE_CHECKING

from litestar.exceptions import TooManyRequestsException

if TYPE_CHECKING:
    from datetime import timedelta

from pinch_backend.auth.models import AuthAttempt
from pinch_backend.models import utcnow
from pinch_backend.observability import get_logger

log = get_logger(__name__)


async def require_within_limit(key: str, *, limit: int, window: timedelta) -> None:
    """Record an attempt under ``key``; 429 once the window is full.

    Attempts are counted, not failures — a credential-stuffing run doesn't
    get free tries by occasionally succeeding. Rejected requests are not
    recorded, so a limited principal's window can actually drain.
    """
    cutoff = utcnow() - window
    await AuthAttempt.where(lambda a: (a.key == key) & (a.created_at <= cutoff)).delete()
    recent = await AuthAttempt.where(lambda a: (a.key == key) & (a.created_at > cutoff)).count()
    if recent >= limit:
        log.info("auth.rate_limited", key=key, limit=limit)
        raise TooManyRequestsException(detail="Too many attempts; try again later")
    await AuthAttempt.create(key=key)
