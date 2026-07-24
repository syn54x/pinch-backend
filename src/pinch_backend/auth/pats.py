"""PAT issuance and resolution — the second credential (PRD M3, issue #10).

The shape of sessions.py, deliberately: the secret leaves this module
exactly once, in the return value the create endpoint hands to the client;
the database only ever sees its hash.
"""

from datetime import timedelta
from typing import TYPE_CHECKING

from pinch_backend.auth.models import PatScope, PersonalAccessToken
from pinch_backend.auth.tokens import generate_token, hash_token
from pinch_backend.models import utcnow

if TYPE_CHECKING:
    from pinch_backend.models import User

PAT_PREFIX = "pinch_pat_"
"""The locked secret format (#8): distinctive and regexable for secret
scanners, underscores so a double-click selects the whole token. Cosmetic
to verification — the hash covers the whole string."""

_DISPLAY_CHARS = 4
"""Random characters shown after the prefix in list views: enough to match
a leaked token to a row, a rounding error against the 43 the secret has."""

_TOUCH_GRANULARITY = timedelta(minutes=1)
"""Floor between last-used writes, same rationale as sessions' last-seen:
the list view's liveness signal doesn't need per-request UPDATEs."""


async def issue_pat(
    user: User, *, name: str, scope: PatScope, penny: bool = False
) -> tuple[PersonalAccessToken, str]:
    """Mint a PAT for ``user``; returns the row and the one-time secret."""
    token = generate_token(prefix=PAT_PREFIX)
    pat = await PersonalAccessToken.create(
        user=user,
        name=name,
        scope=scope,
        penny_scope=penny,
        token_hash=token.token_hash,
        display_prefix=token.secret[: len(PAT_PREFIX) + _DISPLAY_CHARS],
    )
    return pat, token.secret


async def resolve_pat(secret: str) -> PersonalAccessToken | None:
    """Return the PAT behind a presented bearer secret, or None.

    The whole string is hashed, never parsed (the format is cosmetic to
    verification). No expiry checks: PATs don't expire (deliberately out of
    M3 scope), so a row that exists is live — revocation deletes it.
    """
    pat = await PersonalAccessToken.where(lambda p: p.token_hash == hash_token(secret)).first()
    if pat is None:
        return None
    now = utcnow()
    if pat.last_used_at is None or now - pat.last_used_at >= _TOUCH_GRANULARITY:
        pat.last_used_at = now
        await pat.save()
    return pat
