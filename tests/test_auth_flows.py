"""M2 CP4 seam: session management and mailed-token flows (issue #4).

HTTP-seam tests. The console mailer's stdout IS the delivery channel, so
tests capture it to click the mailed link the way a user would; HIBP is
exercised only through a stubbed httpx transport (no live network in CI).
"""

import hashlib
import re
from datetime import timedelta

import httpx
import pytest
from litestar.exceptions import PermissionDeniedException

from pinch_backend.auth import breach
from pinch_backend.auth.guards import provide_current_ledger
from pinch_backend.auth.models import PasswordResetToken, Session
from pinch_backend.auth.passwords import hash_password
from pinch_backend.auth.sessions import issue_session
from pinch_backend.mailer import get_mailer
from pinch_backend.models import User, provision_user, utcnow
from pinch_backend.settings import settings

SIGNUP = "/api/v1/auth/signup"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
SESSIONS = "/api/v1/auth/sessions"
VERIFY_REQUEST = "/api/v1/auth/email-verification/request"
VERIFY_CONFIRM = "/api/v1/auth/email-verification/confirm"
RESET_REQUEST = "/api/v1/auth/password-reset/request"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com", **overrides):
    payload = {"email": email, "password": PASSWORD, "display_name": "Taylor", **overrides}
    return await client.post(SIGNUP, json=payload, headers=await _csrf(client))


def _mailed_token(capsys) -> str:
    """Pull the most recently mailed link's token off the console mailer."""
    tokens = re.findall(r"token=([A-Za-z0-9_-]+)", capsys.readouterr().out)
    assert tokens, "expected the console mailer to have delivered a link"
    return tokens[-1]


# --- Session list / revoke (PRD story 6) -------------------------------------


async def test_session_list_shows_created_last_seen_and_client_hint(client) -> None:
    await _signup(client)
    response = await client.get(SESSIONS)
    assert response.status_code == 200
    (session,) = response.json()["items"]
    assert set(session) == {"id", "created_at", "last_seen_at", "client_hint", "current"}
    assert session["current"] is True


async def test_each_login_is_its_own_revocable_session(client) -> None:
    await _signup(client)
    first_id = (await client.get(SESSIONS)).json()["items"][0]["id"]

    # A "second device" logs in: same account, new session.
    await client.post(
        LOGIN,
        json={"email": "taylor@example.com", "password": PASSWORD},
        headers={**await _csrf(client), "user-agent": "Penny on iOS"},
    )
    sessions = (await client.get(SESSIONS)).json()["items"]
    assert len(sessions) == 2
    current = [s for s in sessions if s["current"]]
    assert len(current) == 1
    assert current[0]["id"] != first_id
    assert current[0]["client_hint"] == "Penny on iOS"


async def test_revoking_a_session_kills_it_server_side(client) -> None:
    signup_response = await _signup(client)
    stolen = (
        next(
            h
            for h in signup_response.headers.get_list("set-cookie")
            if h.startswith(f"{settings.session_cookie_name}=")
        )
        .split(";")[0]
        .split("=", 1)[1]
    )
    first_id = (await client.get(SESSIONS)).json()["items"][0]["id"]

    await client.post(
        LOGIN,
        json={"email": "taylor@example.com", "password": PASSWORD},
        headers=await _csrf(client),
    )
    assert (
        await client.delete(SESSIONS + f"/{first_id}", headers=await _csrf(client))
    ).status_code == 204

    # The library computer's forgotten login (PRD story 6) is dead.
    client.cookies.set(settings.session_cookie_name, stolen, domain="testserver.local")
    assert (await client.get(ME)).status_code == 401


async def test_cannot_revoke_a_session_you_do_not_own(client) -> None:
    await _signup(client)
    other = await provision_user(
        email="other@example.com", display_name="Other", password_hash=hash_password(PASSWORD)
    )
    other_session, _ = await issue_session(other)

    response = await client.delete(SESSIONS + f"/{other_session.id}", headers=await _csrf(client))
    # 404, not 403: the response must not confirm the session exists.
    assert response.status_code == 404
    assert await Session.where(lambda s: s.id == other_session.id).count() == 1


async def test_expired_sessions_do_not_appear_in_the_list(client) -> None:
    await _signup(client)
    user = (await User.all())[0]
    stale, _ = await issue_session(user)
    stale.last_seen_at = utcnow() - settings.session_idle_ttl - timedelta(seconds=1)
    await stale.save()

    sessions = (await client.get(SESSIONS)).json()["items"]
    assert [s["current"] for s in sessions] == [True]


async def test_revoking_an_unknown_session_is_404(client) -> None:
    await _signup(client)
    response = await client.delete(
        SESSIONS + "/018f0000-0000-7000-8000-000000000000", headers=await _csrf(client)
    )
    assert response.status_code == 404


# --- Email verification (PRD story 8) ----------------------------------------


async def test_signup_mails_a_verification_link_that_verifies(client, capsys) -> None:
    await _signup(client)
    assert (await client.get(ME)).json()["email_verified"] is False

    token = _mailed_token(capsys)
    confirm = await client.post(VERIFY_CONFIRM, json={"token": token}, headers=await _csrf(client))
    assert confirm.status_code == 204
    assert (await client.get(ME)).json()["email_verified"] is True


async def test_verification_can_be_rerequested(client, capsys) -> None:
    await _signup(client)
    capsys.readouterr()  # discard the signup mail

    assert (await client.post(VERIFY_REQUEST, headers=await _csrf(client))).status_code == 202
    token = _mailed_token(capsys)
    assert (
        await client.post(VERIFY_CONFIRM, json={"token": token}, headers=await _csrf(client))
    ).status_code == 204


async def test_verification_token_is_single_use(client, capsys) -> None:
    await _signup(client)
    token = _mailed_token(capsys)
    headers = await _csrf(client)
    assert (
        await client.post(VERIFY_CONFIRM, json={"token": token}, headers=headers)
    ).status_code == 204
    assert (
        await client.post(VERIFY_CONFIRM, json={"token": token}, headers=headers)
    ).status_code == 400


async def test_expired_or_garbage_verification_tokens_fail_alike(client, capsys) -> None:
    from pinch_backend.auth.models import EmailVerificationToken

    await _signup(client)
    token = _mailed_token(capsys)
    row = (await EmailVerificationToken.all())[0]
    row.expires_at = utcnow() - timedelta(seconds=1)
    await row.save()

    headers = await _csrf(client)
    expired = await client.post(VERIFY_CONFIRM, json={"token": token}, headers=headers)
    garbage = await client.post(VERIFY_CONFIRM, json={"token": "not-a-token"}, headers=headers)
    assert expired.status_code == garbage.status_code == 400
    assert expired.json() == garbage.json()


# --- Password reset (PRD story 9) ---------------------------------------------


async def test_password_reset_end_to_end(client, capsys) -> None:
    await _signup(client)
    await client.post(LOGOUT, headers=await _csrf(client))
    capsys.readouterr()

    assert (
        await client.post(
            RESET_REQUEST, json={"email": "taylor@example.com"}, headers=await _csrf(client)
        )
    ).status_code == 202
    token = _mailed_token(capsys)

    new_password = "entirely new horse staple"
    assert (
        await client.post(
            RESET_CONFIRM,
            json={"token": token, "password": new_password},
            headers=await _csrf(client),
        )
    ).status_code == 204

    old = await client.post(
        LOGIN,
        json={"email": "taylor@example.com", "password": PASSWORD},
        headers=await _csrf(client),
    )
    assert old.status_code == 401
    new = await client.post(
        LOGIN,
        json={"email": "taylor@example.com", "password": new_password},
        headers=await _csrf(client),
    )
    assert new.status_code == 200


async def test_reset_invalidates_every_session(client, capsys) -> None:
    await _signup(client)  # leaves a live session in the cookie jar
    capsys.readouterr()
    await client.post(
        RESET_REQUEST, json={"email": "taylor@example.com"}, headers=await _csrf(client)
    )
    token = _mailed_token(capsys)
    await client.post(
        RESET_CONFIRM,
        json={"token": token, "password": "entirely new horse staple"},
        headers=await _csrf(client),
    )

    # PRD story 9: recovery from compromise is one action.
    assert (await client.get(ME)).status_code == 401
    assert await Session.select().count() == 0


async def test_reset_request_does_not_reveal_whether_an_email_exists(client) -> None:
    await _signup(client)
    headers = await _csrf(client)
    known = await client.post(RESET_REQUEST, json={"email": "taylor@example.com"}, headers=headers)
    unknown = await client.post(
        RESET_REQUEST, json={"email": "nobody@example.com"}, headers=headers
    )
    assert known.status_code == unknown.status_code == 202
    assert known.content == unknown.content


async def test_a_completed_reset_consumes_all_outstanding_reset_tokens(client, capsys) -> None:
    await _signup(client)
    headers = await _csrf(client)
    capsys.readouterr()

    await client.post(RESET_REQUEST, json={"email": "taylor@example.com"}, headers=headers)
    first_token = _mailed_token(capsys)
    await client.post(RESET_REQUEST, json={"email": "taylor@example.com"}, headers=headers)
    second_token = _mailed_token(capsys)

    assert (
        await client.post(
            RESET_CONFIRM,
            json={"token": second_token, "password": "entirely new horse staple"},
            headers=headers,
        )
    ).status_code == 204
    # The attacker's older, unused link is dead too.
    assert (
        await client.post(
            RESET_CONFIRM,
            json={"token": first_token, "password": "attacker chosen password"},
            headers=headers,
        )
    ).status_code == 400
    assert all(t.consumed_at is not None for t in await PasswordResetToken.all())


async def test_reset_confirm_enforces_the_password_floor(client, capsys) -> None:
    await _signup(client)
    capsys.readouterr()
    headers = await _csrf(client)
    await client.post(RESET_REQUEST, json={"email": "taylor@example.com"}, headers=headers)
    token = _mailed_token(capsys)

    response = await client.post(
        RESET_CONFIRM, json={"token": token, "password": "short"}, headers=headers
    )
    assert response.status_code == 400


# --- HIBP breach check (PRD story 2; stubbed transport, no live network) ------


def _hibp_transport(breached_password: str | None, calls: list[str] | None = None):
    """A stubbed pwnedpasswords range API: optionally 'knows' one password."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        lines = ["0000000000000000000000000000000000A:5"]
        if breached_password is not None:
            sha1 = hashlib.sha1(breached_password.encode()).hexdigest().upper()
            lines.append(f"{sha1[5:]}:1337")
        return httpx.Response(200, text="\r\n".join(lines))

    return httpx.MockTransport(handler)


async def test_a_breached_password_is_rejected_at_signup(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", _hibp_transport(breached_password=PASSWORD))
    response = await _signup(client)
    assert response.status_code == 400
    assert "breach" in response.json()["detail"].lower()


async def test_an_unbreached_password_passes_the_check(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", _hibp_transport(breached_password=None))
    assert (await _signup(client)).status_code == 201


async def test_only_a_five_char_prefix_ever_leaves_the_process(client, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", _hibp_transport(None, calls))
    await _signup(client)

    sha1 = hashlib.sha1(PASSWORD.encode()).hexdigest().upper()
    assert calls == [f"/range/{sha1[:5]}"]


async def test_hibp_outage_fails_open_and_says_so_in_the_log(client, monkeypatch, capsys) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", httpx.MockTransport(unreachable))

    response = await _signup(client)
    assert response.status_code == 201  # availability over ceremony
    assert "auth.breach_check.skipped" in capsys.readouterr().out


async def test_disabled_breach_check_makes_no_network_call(client, monkeypatch) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("breach check must not be called when disabled")

    monkeypatch.setattr(breach, "_transport", httpx.MockTransport(explode))
    assert (await _signup(client)).status_code == 201  # flag is off in conftest


async def test_reset_confirm_also_breach_checks_the_new_password(
    client, monkeypatch, capsys
) -> None:
    await _signup(client)
    capsys.readouterr()
    headers = await _csrf(client)
    await client.post(RESET_REQUEST, json={"email": "taylor@example.com"}, headers=headers)
    token = _mailed_token(capsys)

    breached = "entirely new horse staple"
    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", _hibp_transport(breached_password=breached))
    response = await client.post(
        RESET_CONFIRM, json={"token": token, "password": breached}, headers=headers
    )
    assert response.status_code == 400


# --- The verification gate (PRD story 10; exercised via config in CP5) --------


async def test_current_ledger_requires_verification_when_configured(db, monkeypatch) -> None:
    user = await provision_user(
        email="taylor@example.com", display_name="Taylor", password_hash=hash_password(PASSWORD)
    )
    monkeypatch.setattr(settings, "verification_required", True)
    with pytest.raises(PermissionDeniedException):
        await provide_current_ledger(user)

    user.email_verified_at = utcnow()
    await user.save()
    assert (await provide_current_ledger(user)).id is not None


# --- Mailer -------------------------------------------------------------------


def test_an_unknown_mailer_backend_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mailer_backend", "smtp")
    with pytest.raises(LookupError, match="smtp"):
        get_mailer()
