"""M2 CP1 seam: auth schema and primitives (PRD M2, issue #4).

Model-layer tests in the style of test_domain.py: each asserts a PRD
implementation decision — argon2id with pinned parameters, 256-bit tokens
stored only as hashes, session idle/absolute expiry semantics, single-use
token tables. CP3's HTTP-seam tests re-cover the flows from above; these
pin the primitives those flows are built on.
"""

from datetime import timedelta

import pytest
from argon2.exceptions import InvalidHashError
from ferro import UniqueViolationError, evict_instance

from pinch_backend.auth.models import EmailVerificationToken, PasswordResetToken, Session
from pinch_backend.auth.passwords import hash_password, needs_rehash, verify_password
from pinch_backend.auth.tokens import generate_token, hash_token
from pinch_backend.models import provision_user, utcnow

# --- Password hashing ------------------------------------------------------


def test_password_verifies_round_trip() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password(stored, "correct horse battery staple")


def test_wrong_password_fails_verification() -> None:
    stored = hash_password("correct horse battery staple")
    assert not verify_password(stored, "incorrect horse battery staple")


def test_same_password_hashes_differently_each_time() -> None:
    # Per-hash random salt: equal passwords must not produce equal hashes.
    assert hash_password("hunter2hunter2") != hash_password("hunter2hunter2")


def test_hashing_is_argon2id_with_the_pinned_parameters() -> None:
    """Deliberate format assertion (contra the no-hash-format rule): the PRD
    pins argon2id with documented parameters, and this test is what stops a
    library upgrade from drifting them silently."""
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert not needs_rehash(stored)


def test_hash_under_old_parameters_reports_needing_rehash() -> None:
    from argon2 import PasswordHasher

    weaker = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("hunter2hunter2")
    assert needs_rehash(weaker)


def test_malformed_stored_hash_raises_instead_of_returning_false() -> None:
    # A corrupt hash column is a bug to surface, not a failed login (I-1).
    with pytest.raises(InvalidHashError):
        verify_password("not-an-argon2-hash", "whatever")


# --- Opaque tokens ---------------------------------------------------------


def test_tokens_are_unique_and_256_bit() -> None:
    first, second = generate_token(), generate_token()
    assert first.secret != second.secret
    # 32 bytes url-safe-encoded: 43 chars, no padding.
    assert len(first.secret) >= 43


def test_stored_hash_derives_from_secret_but_never_equals_it() -> None:
    token = generate_token()
    assert token.token_hash == hash_token(token.secret)
    assert token.token_hash != token.secret


# --- Session table ---------------------------------------------------------


async def test_session_round_trips_and_is_reachable_by_token_hash(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    token = generate_token()

    session = await Session.create(
        user=user,
        token_hash=token.token_hash,
        client_hint="Firefox on macOS",
        absolute_expires_at=utcnow() + timedelta(days=30),
    )
    evict_instance("Session", str(session.id))

    fetched = await Session.where(lambda s: s.token_hash == token.token_hash).first()
    assert fetched.user_id == user.id
    assert fetched.client_hint == "Firefox on macOS"
    assert fetched.last_seen_at.utcoffset() == timedelta(0)
    assert (await user.sessions.all()) == [fetched]


async def test_session_token_hash_is_unique(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    token = generate_token()
    expires = utcnow() + timedelta(days=30)

    await Session.create(user=user, token_hash=token.token_hash, absolute_expires_at=expires)
    with pytest.raises(UniqueViolationError):
        await Session.create(user=user, token_hash=token.token_hash, absolute_expires_at=expires)


async def test_session_expires_idle_and_absolute(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    now = utcnow()
    idle_ttl = timedelta(hours=12)
    session = await Session.create(
        user=user,
        token_hash=generate_token().token_hash,
        absolute_expires_at=now + timedelta(days=30),
    )

    assert session.is_active(idle_ttl=idle_ttl, now=now)
    # Idle expiry: no activity for longer than the idle TTL.
    assert not session.is_active(idle_ttl=idle_ttl, now=now + timedelta(hours=13))
    # Absolute expiry: even a continuously active session dies at the deadline.
    session.last_seen_at = now + timedelta(days=31)
    assert not session.is_active(idle_ttl=idle_ttl, now=now + timedelta(days=31))


async def test_session_repr_never_contains_the_token_hash(db) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    token = generate_token()
    session = await Session.create(
        user=user,
        token_hash=token.token_hash,
        absolute_expires_at=utcnow() + timedelta(days=30),
    )
    assert token.token_hash not in repr(session)
    assert token.token_hash not in str(session)


# --- Verification / reset token tables -------------------------------------


@pytest.mark.parametrize("token_model", [EmailVerificationToken, PasswordResetToken])
async def test_flow_tokens_round_trip_unconsumed(db, token_model) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    token = generate_token()

    row = await token_model.create(
        user=user,
        token_hash=token.token_hash,
        expires_at=utcnow() + timedelta(hours=1),
    )
    evict_instance(token_model.__name__, str(row.id))

    fetched = await token_model.where(lambda t: t.token_hash == token.token_hash).first()
    assert fetched.user_id == user.id
    assert fetched.consumed_at is None
    assert token.token_hash not in repr(fetched)


@pytest.mark.parametrize("token_model", [EmailVerificationToken, PasswordResetToken])
async def test_flow_token_hashes_are_unique(db, token_model) -> None:
    user = await provision_user(email="taylor@example.com", display_name="Taylor")
    token = generate_token()
    expires = utcnow() + timedelta(hours=1)

    await token_model.create(user=user, token_hash=token.token_hash, expires_at=expires)
    with pytest.raises(UniqueViolationError):
        await token_model.create(user=user, token_hash=token.token_hash, expires_at=expires)
