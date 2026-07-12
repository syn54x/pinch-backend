"""PAT issuance — minting the second credential (PRD M3, issue #10).

The shape of sessions.py, deliberately: the secret leaves this module
exactly once, in the return value the create endpoint hands to the client;
the database only ever sees its hash. Resolution (bearer → user) lives with
the guards it feeds, not here.
"""

from typing import TYPE_CHECKING

from pinch_backend.auth.models import PatScope, PersonalAccessToken
from pinch_backend.auth.tokens import generate_token

if TYPE_CHECKING:
    from pinch_backend.models import User

PAT_PREFIX = "pinch_pat_"
"""The locked secret format (#8): distinctive and regexable for secret
scanners, underscores so a double-click selects the whole token. Cosmetic
to verification — the hash covers the whole string."""

_DISPLAY_CHARS = 4
"""Random characters shown after the prefix in list views: enough to match
a leaked token to a row, a rounding error against the 43 the secret has."""


async def issue_pat(user: User, *, name: str, scope: PatScope) -> tuple[PersonalAccessToken, str]:
    """Mint a PAT for ``user``; returns the row and the one-time secret."""
    token = generate_token(prefix=PAT_PREFIX)
    pat = await PersonalAccessToken.create(
        user=user,
        name=name,
        scope=scope,
        token_hash=token.token_hash,
        display_prefix=token.secret[: len(PAT_PREFIX) + _DISPLAY_CHARS],
    )
    return pat, token.secret
