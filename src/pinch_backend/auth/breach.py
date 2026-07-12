"""HIBP k-anonymity breach check (PRD M2 story 2).

Only the first five hex characters of the password's SHA-1 ever leave the
process (SHA-1 because the pwnedpasswords protocol says so — it names
corpus entries, it protects nothing). Fails open on network trouble:
availability over ceremony, and the skip is logged so it's observable.
"""

import hashlib

import httpx

from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)

_RANGE_URL = "https://api.pwnedpasswords.com/range/"

_transport: httpx.AsyncBaseTransport | None = None
"""Tests inject an httpx.MockTransport here; production leaves it None."""


async def password_is_breached(password: str) -> bool:
    """True when the password appears in known breach corpora.

    False when the check is disabled by config or HIBP is unreachable
    (fail-open, logged) — never an exception on the signup path.
    """
    if not settings.breach_check_enabled:
        return False
    digest = hashlib.sha1(password.encode()).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=3.0) as client:
            response = await client.get(_RANGE_URL + prefix)
            response.raise_for_status()
    except httpx.HTTPError as error:
        log.warning("auth.breach_check.skipped", reason=type(error).__name__)
        return False
    return any(line.split(":")[0] == suffix for line in response.text.splitlines())
