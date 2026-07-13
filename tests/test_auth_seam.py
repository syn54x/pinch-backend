"""M2 CP2 seam: the login-method registry and session issuance (issue #4).

The ADR-0005 structural commitment under test: every login method is
`credentials → verified identity`, and every confirmed identity terminates
in the same issue_session(). CP3's HTTP tests re-cover these flows from
above; these pin the seam's contract directly.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from litestar.exceptions import NotAuthorizedException
from pydantic import ValidationError

from pinch_backend.auth import methods
from pinch_backend.auth.guards import (
    provide_current_credential,
    provide_current_ledger,
    provide_current_session,
    provide_current_user,
)
from pinch_backend.auth.models import Session
from pinch_backend.auth.passwords import hash_password, needs_rehash
from pinch_backend.auth.sessions import (
    TOUCH_GRANULARITY,
    clear_session_cookie,
    issue_session,
    resolve_session,
    session_cookie,
)
from pinch_backend.auth.tokens import hash_token
from pinch_backend.models import Ledger, User, provision_user, utcnow
from pinch_backend.settings import Settings, settings

PASSWORD = "correct horse battery staple"


async def _signup(email: str = "taylor@example.com") -> User:
    return await provision_user(
        email=email, display_name="Taylor", password_hash=hash_password(PASSWORD)
    )


# --- Settings: secret key --------------------------------------------------


def test_secret_key_is_required_outside_development() -> None:
    with pytest.raises(ValidationError, match="PINCH_SECRET_KEY"):
        Settings(environment="production", secret_key="")


def test_development_generates_an_ephemeral_secret_key() -> None:
    generated = Settings(environment="development", secret_key="")
    assert generated.secret_key
    assert generated.secret_key != Settings(environment="development", secret_key="").secret_key


# --- Login-method registry -------------------------------------------------


def test_v0_registers_exactly_the_password_method() -> None:
    assert methods.registered() == ("password",)


def test_unknown_method_lookup_fails_loudly() -> None:
    with pytest.raises(LookupError, match="oidc"):
        methods.get("oidc")


def test_double_registration_is_an_error() -> None:
    with pytest.raises(ValueError, match="password"):
        methods.register(methods.PasswordMethod())


# --- Password method -------------------------------------------------------


async def test_correct_credentials_confirm_the_identity(db) -> None:
    user = await _signup()
    confirmed = await methods.get("password").authenticate(
        {"email": "taylor@example.com", "password": PASSWORD}
    )
    assert confirmed is not None
    assert confirmed.id == user.id


async def test_email_lookup_is_case_insensitive(db) -> None:
    user = await _signup()
    confirmed = await methods.get("password").authenticate(
        {"email": "Taylor@EXAMPLE.com", "password": PASSWORD}
    )
    assert confirmed is not None
    assert confirmed.id == user.id


async def test_wrong_password_confirms_nothing(db) -> None:
    await _signup()
    confirmed = await methods.get("password").authenticate(
        {"email": "taylor@example.com", "password": "incorrect horse"}
    )
    assert confirmed is None


async def test_unknown_email_confirms_nothing(db) -> None:
    await _signup()
    confirmed = await methods.get("password").authenticate(
        {"email": "nobody@example.com", "password": PASSWORD}
    )
    assert confirmed is None


async def test_a_passwordless_user_cannot_password_login(db) -> None:
    # password_hash=None (a future social-only account) must behave exactly
    # like an unknown email, not error.
    await provision_user(email="social@example.com", display_name="Social")
    confirmed = await methods.get("password").authenticate(
        {"email": "social@example.com", "password": PASSWORD}
    )
    assert confirmed is None


async def test_malformed_credentials_raise_rather_than_confirm(db) -> None:
    with pytest.raises(ValidationError):
        await methods.get("password").authenticate({"email": "taylor@example.com"})


async def test_successful_login_upgrades_a_stale_hash(db) -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    user = await provision_user(
        email="taylor@example.com", display_name="Taylor", password_hash=weak
    )
    assert needs_rehash(user.password_hash)

    confirmed = await methods.get("password").authenticate(
        {"email": "taylor@example.com", "password": PASSWORD}
    )
    assert confirmed is not None
    assert not needs_rehash(confirmed.password_hash)
    # And the upgraded hash still verifies on the next login.
    again = await methods.get("password").authenticate(
        {"email": "taylor@example.com", "password": PASSWORD}
    )
    assert again is not None and again.id == user.id


# --- Session issuance and resolution ---------------------------------------


async def test_issue_session_hands_out_the_secret_and_stores_only_its_hash(db) -> None:
    user = await _signup()
    session, secret = await issue_session(user, client_hint="Firefox on macOS")

    assert session.token_hash == hash_token(secret)
    assert session.token_hash != secret
    assert session.client_hint == "Firefox on macOS"
    expected = utcnow() + settings.session_absolute_ttl
    assert abs((session.absolute_expires_at - expected).total_seconds()) < 5


async def test_an_issued_session_resolves_by_its_secret(db) -> None:
    user = await _signup()
    session, secret = await issue_session(user)

    resolved = await resolve_session(secret)
    assert resolved is not None
    assert resolved.id == session.id
    assert resolved.user_id == user.id


async def test_an_unknown_secret_resolves_to_nothing(db) -> None:
    await _signup()
    assert await resolve_session("not-a-real-secret") is None


async def test_an_idle_expired_session_is_dead_and_gone(db) -> None:
    user = await _signup()
    session, secret = await issue_session(user)
    session.last_seen_at = utcnow() - settings.session_idle_ttl - timedelta(seconds=1)
    await session.save()

    assert await resolve_session(secret) is None
    assert await Session.select().count() == 0


async def test_an_absolutely_expired_session_is_dead_even_when_active(db) -> None:
    user = await _signup()
    session, secret = await issue_session(user)
    session.absolute_expires_at = utcnow() - timedelta(seconds=1)
    await session.save()

    assert await resolve_session(secret) is None
    assert await Session.select().count() == 0


async def test_resolution_touches_last_seen_only_past_the_granularity(db) -> None:
    user = await _signup()
    session, secret = await issue_session(user)

    # A brand-new session is inside the granularity window: no write.
    resolved = await resolve_session(secret)
    assert resolved.last_seen_at == session.last_seen_at

    resolved.last_seen_at = utcnow() - TOUCH_GRANULARITY - timedelta(seconds=1)
    await resolved.save()
    touched = await resolve_session(secret)
    assert (utcnow() - touched.last_seen_at) < TOUCH_GRANULARITY


async def test_an_oversized_client_hint_is_trimmed_not_rejected(db) -> None:
    user = await _signup()
    session, _ = await issue_session(user, client_hint="x" * 10_000)
    assert session.client_hint is not None
    assert len(session.client_hint) <= 256


# --- Session cookie shape ---------------------------------------------------


def test_session_cookie_is_httponly_secure_lax_and_bounded() -> None:
    cookie = session_cookie("the-secret")
    assert cookie.key == settings.session_cookie_name
    assert cookie.value == "the-secret"
    assert cookie.httponly is True
    assert cookie.secure is True
    assert cookie.samesite == "lax"
    assert cookie.path == "/"
    assert cookie.max_age == int(settings.session_absolute_ttl.total_seconds())


def test_clearing_the_session_cookie_expires_it_immediately() -> None:
    cookie = clear_session_cookie()
    assert cookie.key == settings.session_cookie_name
    assert cookie.value == ""
    assert cookie.max_age == 0
    assert cookie.httponly is True


# --- Guards (core behavior; the HTTP seam re-covers these in CP3) ----------


def _request(cookies: dict[str, str]) -> SimpleNamespace:
    """The slice of Request the credential resolver reads."""
    return SimpleNamespace(cookies=cookies, headers={}, method="GET", client=None)


async def test_the_credential_chain_resolves_from_the_cookie(db) -> None:
    user = await _signup()
    session, secret = await issue_session(user)

    credential = await provide_current_credential(_request({settings.session_cookie_name: secret}))
    resolved = await provide_current_session(credential)
    assert resolved.id == session.id
    assert (await provide_current_user(credential)).id == user.id


@pytest.mark.parametrize("cookies", [{}, {"pinch_session": "forged-or-stale"}])
async def test_no_valid_session_means_not_authenticated(db, cookies) -> None:
    await _signup()
    with pytest.raises(NotAuthorizedException):
        await provide_current_credential(_request(cookies))


async def test_current_ledger_is_the_provisioned_ledger(db) -> None:
    user = await _signup()
    ledger = await provide_current_ledger(user)
    assert isinstance(ledger, Ledger)
    assert ledger.id == (await Ledger.all())[0].id


async def test_a_member_of_no_ledger_is_a_loud_invariant_violation(db) -> None:
    # provision_user makes this impossible; if it ever happens, 500 — never
    # a silent empty ledger.
    orphan = await User.create(email="orphan@example.com", display_name="Orphan")
    with pytest.raises(RuntimeError, match="no ledger membership"):
        await provide_current_ledger(orphan)
