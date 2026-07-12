"""M3 CP2 seam: PAT schema and issuance primitives (issue #10).

Model-layer tests in the style of test_auth_primitives.py: each asserts a
PRD implementation decision — the locked ``pinch_pat_`` secret format, the
whole-string hash (format is cosmetic to verification), plaintext display
prefix, hashed-at-rest storage. CP3's HTTP-seam tests cover the endpoints
these primitives are built for.
"""

import re

import pytest
from ferro import UniqueViolationError, evict_instance

from pinch_backend.auth.models import PatScope, PersonalAccessToken
from pinch_backend.auth.pats import PAT_PREFIX, issue_pat
from pinch_backend.auth.tokens import generate_token, hash_token
from pinch_backend.models import provision_user

# --- The locked secret format ------------------------------------------------


def test_pat_secrets_have_the_locked_format() -> None:
    """pinch_pat_<token_urlsafe(32)>: a distinctive, regexable prefix with
    underscores (the GitHub ghp_ double-click rationale, locked in #8)."""
    token = generate_token(prefix=PAT_PREFIX)
    assert re.fullmatch(r"pinch_pat_[A-Za-z0-9_-]{43}", token.secret)


def test_pat_secrets_are_unique() -> None:
    assert generate_token(prefix=PAT_PREFIX).secret != generate_token(prefix=PAT_PREFIX).secret


def test_the_hash_covers_the_whole_string_never_a_parsed_part() -> None:
    """The server hashes the full secret and never parses it — the format is
    cosmetic to verification, so future prefixes need no migration."""
    token = generate_token(prefix=PAT_PREFIX)
    assert token.token_hash == hash_token(token.secret)
    bare = token.secret.removeprefix(PAT_PREFIX)
    assert token.token_hash != hash_token(bare)


def test_a_prefixless_token_is_unchanged() -> None:
    """M2 call sites keep their exact behavior: no prefix by default."""
    token = generate_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token.secret)


# --- The PAT table -------------------------------------------------------------


async def test_issue_pat_stores_the_hash_and_display_prefix_never_the_secret(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    pat, secret = await issue_pat(user, name="ci-script", scope=PatScope.READ)

    assert pat.token_hash == hash_token(secret)
    assert pat.token_hash != secret
    # The display prefix is the head of the secret — enough to match a leaked
    # token against the list, useless to authenticate with.
    assert secret.startswith(pat.display_prefix)
    assert pat.display_prefix.startswith(PAT_PREFIX)
    assert len(pat.display_prefix) < len(PAT_PREFIX) + 8
    assert pat.name == "ci-script"
    assert pat.scope is PatScope.READ
    assert pat.last_used_at is None


async def test_pat_round_trips_and_is_reachable_by_token_hash(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    pat, secret = await issue_pat(user, name="ci-script", scope=PatScope.WRITE)
    evict_instance("PersonalAccessToken", str(pat.id))

    fetched = await PersonalAccessToken.where(lambda p: p.token_hash == hash_token(secret)).first()
    assert fetched.user_id == user.id
    assert fetched.scope is PatScope.WRITE
    assert (await user.personal_access_tokens.all()) == [fetched]


async def test_pat_token_hashes_are_unique(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    token = generate_token(prefix=PAT_PREFIX)

    await PersonalAccessToken.create(
        user=user, name="one", scope=PatScope.READ, token_hash=token.token_hash, display_prefix="x"
    )
    with pytest.raises(UniqueViolationError):
        await PersonalAccessToken.create(
            user=user,
            name="two",
            scope=PatScope.READ,
            token_hash=token.token_hash,
            display_prefix="x",
        )


async def test_pat_repr_never_contains_the_token_hash(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    pat, secret = await issue_pat(user, name="ci-script", scope=PatScope.READ)
    for rendered in (repr(pat), str(pat)):
        assert pat.token_hash not in rendered
        assert secret not in rendered
