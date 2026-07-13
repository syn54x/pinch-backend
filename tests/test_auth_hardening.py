"""M2 CP5 seam: the config matrix and the secrets discipline (issue #4).

Hosted vs self-host is config, never a fork (ADR-0002): the same app, under
different settings, is exercised over HTTP both ways — via a probe route
that consumes ``current_ledger``, the gateway every M3 endpoint will use.
The journey test then proves the structured events exist and that no
password, token, cookie secret, or hash ever reaches a structured log line.
"""

import json
import re
from datetime import timedelta

import httpx
import pytest
from litestar import get
from litestar.di import NamedDependency
from litestar.testing import AsyncTestClient

from pinch_backend.api.app import create_app
from pinch_backend.auth import breach
from pinch_backend.auth.tokens import hash_token
from pinch_backend.models import Ledger, User
from pinch_backend.settings import Settings, settings

SIGNUP = "/api/v1/auth/signup"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
SESSIONS = "/api/v1/auth/sessions"
PATS = "/api/v1/auth/pats"
VERIFY_CONFIRM = "/api/v1/auth/email-verification/confirm"
RESET_REQUEST = "/api/v1/auth/password-reset/request"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"

PASSWORD = "correct horse battery staple"


@get("/_probe/ledger")
async def ledger_probe(current_ledger: NamedDependency[Ledger]) -> dict[str, str]:
    """Test-only stand-in for every M3 domain endpoint: reaching it means
    the session resolved, the user passed the verification gate, and the
    ledger was found."""
    return {"ledger_id": str(current_ledger.id)}


@pytest.fixture
async def probe_client(db):
    app = create_app(manage_database=False)
    app.register(ledger_probe)
    async with AsyncTestClient(app, base_url="https://testserver.local") as c:
        yield c


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com", password: str = PASSWORD):
    return await client.post(
        SIGNUP,
        json={"email": email, "password": password, "display_name": "Taylor"},
        headers=await _csrf(client),
    )


def _clean_hibp() -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, text="0000000000000000000000000000000000A:5")
    )


# --- The config matrix (PRD stories 10 & 11) ----------------------------------


async def test_hosted_profile_gates_the_ledger_until_the_link_is_clicked(
    probe_client, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(settings, "signup_enabled", True)
    monkeypatch.setattr(settings, "verification_required", True)
    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", _clean_hibp())

    assert (await _signup(probe_client)).status_code == 201
    # The auth surface stays reachable unverified; the domain surface does not.
    assert (await probe_client.get(ME)).status_code == 200
    assert (await probe_client.get("/_probe/ledger")).status_code == 403

    token = re.findall(r"token=([A-Za-z0-9_-]+)", capsys.readouterr().out)[-1]
    assert (
        await probe_client.post(
            VERIFY_CONFIRM, json={"token": token}, headers=await _csrf(probe_client)
        )
    ).status_code == 204
    assert (await probe_client.get("/_probe/ledger")).status_code == 200


async def test_self_host_profile_skips_ceremony(probe_client, monkeypatch) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("self-host profile must not call HIBP")

    monkeypatch.setattr(settings, "verification_required", False)
    monkeypatch.setattr(settings, "breach_check_enabled", False)
    monkeypatch.setattr(breach, "_transport", httpx.MockTransport(explode))

    assert (await _signup(probe_client)).status_code == 201
    # Unverified, and nobody cares: the single-user instance just works.
    assert (await probe_client.get("/_probe/ledger")).status_code == 200

    # User #1 locks the door behind themselves.
    monkeypatch.setattr(settings, "signup_enabled", False)
    assert (await _signup(probe_client, email="two@example.com")).status_code == 403


def test_the_toggles_are_environment_config(monkeypatch) -> None:
    monkeypatch.setenv("PINCH_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("PINCH_VERIFICATION_REQUIRED", "true")
    monkeypatch.setenv("PINCH_BREACH_CHECK_ENABLED", "true")
    monkeypatch.setenv("PINCH_SESSION_IDLE_TTL", "P2D")
    loaded = Settings(_env_file=None, secret_key="test-key")
    assert loaded.signup_enabled is False
    assert loaded.verification_required is True
    assert loaded.breach_check_enabled is True
    assert loaded.session_idle_ttl == timedelta(days=2)
    assert loaded.turnstile_enabled is False  # reserved, nothing reads it yet


# --- Secrets discipline (PRD story 14) -----------------------------------------


async def test_validation_errors_never_echo_the_password(client) -> None:
    short = await _signup(client, password="hunter2")
    assert short.status_code == 400
    assert "hunter2" not in short.text

    reset = await client.post(
        RESET_CONFIRM,
        json={"token": "irrelevant", "password": "hunter2"},
        headers=await _csrf(client),
    )
    assert reset.status_code == 400
    assert "hunter2" not in reset.text


async def test_the_full_journey_logs_events_and_never_a_secret(client, monkeypatch, capsys) -> None:
    """One pass through every flow, then two assertions about the log:
    the structured events all fired, and no secret in any form — password,
    cookie secret, mailed token, their hashes, the argon2 hash — appears
    in any structured line."""
    stdout_total = ""

    def drain() -> str:
        nonlocal stdout_total
        chunk = capsys.readouterr().out
        stdout_total += chunk
        return chunk

    def cookie_secret(response) -> str:
        header = next(
            h
            for h in response.headers.get_list("set-cookie")
            if h.startswith(f"{settings.session_cookie_name}=")
        )
        return header.split(";")[0].split("=", 1)[1]

    secrets_seen: set[str] = {PASSWORD}

    # Signup (mails a verification token), one failed login, logout, login.
    signup = await _signup(client)
    secrets_seen.add(cookie_secret(signup))
    verification_token = re.findall(r"token=([A-Za-z0-9_-]+)", drain())[-1]
    secrets_seen.add(verification_token)

    headers = await _csrf(client)
    await client.post(
        LOGIN, json={"email": "taylor@example.com", "password": "wrong password"}, headers=headers
    )
    await client.post(LOGOUT, headers=headers)
    login = await client.post(
        LOGIN, json={"email": "taylor@example.com", "password": PASSWORD}, headers=headers
    )
    secrets_seen.add(cookie_secret(login))

    # Verify the email, then reset the password (revokes everything).
    await client.post(VERIFY_CONFIRM, json={"token": verification_token}, headers=headers)
    await client.post(RESET_REQUEST, json={"email": "taylor@example.com"}, headers=headers)
    reset_token = re.findall(r"token=([A-Za-z0-9_-]+)", drain())[-1]
    secrets_seen.add(reset_token)
    new_password = "an entirely new passphrase"
    secrets_seen.add(new_password)
    await client.post(
        RESET_CONFIRM, json={"token": reset_token, "password": new_password}, headers=headers
    )

    # Back in; a second session appears and gets revoked.
    first = await client.post(
        LOGIN, json={"email": "taylor@example.com", "password": new_password}, headers=headers
    )
    secrets_seen.add(cookie_secret(first))
    first_id = (await client.get(SESSIONS)).json()["items"][0]["id"]
    second = await client.post(
        LOGIN, json={"email": "taylor@example.com", "password": new_password}, headers=headers
    )
    secrets_seen.add(cookie_secret(second))
    await client.delete(SESSIONS + f"/{first_id}", headers=await _csrf(client))

    # The second credential (M3): mint a PAT, act with it, fail a forged
    # one, revoke. The real secret, the forgery, and their hashes all join
    # the greps — an invalid presented token must be as unloggable as a
    # valid one.
    created = await client.post(
        PATS, json={"name": "journey", "scopes": ["read", "write"]}, headers=await _csrf(client)
    )
    pat_secret = created.json()["token"]
    secrets_seen.add(pat_secret)
    bearer = {"Authorization": f"Bearer {pat_secret}"}
    assert (await client.get(ME, headers=bearer)).status_code == 200
    forged = "pinch_pat_" + "F" * 43
    secrets_seen.add(forged)
    assert (await client.get(ME, headers={"Authorization": f"Bearer {forged}"})).status_code == 401
    await client.delete(PATS + f"/{created.json()['id']}", headers=await _csrf(client))

    # Rate limiting fires...
    monkeypatch.setattr(settings, "auth_rate_limit_per_email", 1)
    await client.post(
        LOGIN, json={"email": "probe@example.com", "password": "x" * 12}, headers=headers
    )
    limited = await client.post(
        LOGIN, json={"email": "probe@example.com", "password": "x" * 12}, headers=headers
    )
    assert limited.status_code == 429
    monkeypatch.setattr(settings, "auth_rate_limit_per_email", 10)

    # ...and an HIBP outage fails open, logged.
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    monkeypatch.setattr(settings, "breach_check_enabled", True)
    monkeypatch.setattr(breach, "_transport", httpx.MockTransport(unreachable))
    assert (await _signup(client, email="second@example.com")).status_code == 201

    # Everything a determined grep could find:
    user = await User.where(lambda u: u.email == "taylor@example.com").first()
    secrets_seen.add(user.password_hash)
    secrets_seen.update({hash_token(s) for s in set(secrets_seen) if "$" not in s})

    drain()
    log_lines = [line for line in stdout_total.splitlines() if line.startswith("{")]
    events = {json.loads(line).get("event") for line in log_lines}
    assert {
        "auth.signup",
        "auth.login",
        "auth.login.failed",
        "auth.logout",
        "auth.verification.requested",
        "auth.verification.confirmed",
        "auth.reset.requested",
        "auth.reset.completed",
        "auth.session.revoked",
        "auth.pat.created",
        "auth.pat.revoked",
        "auth.bearer.failed",
        "auth.rate_limited",
        "auth.breach_check.skipped",
    } <= events

    for line in log_lines:
        for secret in secrets_seen:
            assert secret not in line, f"secret material leaked into a log line: {line}"
    # And the argon2 hash never appears anywhere on stdout, mail included.
    assert "$argon2" not in stdout_total
