"""M2 CP3 seam: the public HTTP API — the platform's primary seam (issue #4).

Full auth lifecycles through the ASGI client against a real database.
Every assertion is what a client observes: status codes, cookie
attributes, "this request now fails" — never hash formats or table shapes.
"""

from datetime import timedelta

from pinch_backend.auth.models import AuthAttempt, Session
from pinch_backend.models import utcnow
from pinch_backend.settings import settings

SIGNUP = "/api/v1/auth/signup"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    """Litestar's double-submit CSRF: any response plants the cookie; unsafe
    methods must echo it in the header."""
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com", **overrides):
    payload = {"email": email, "password": PASSWORD, "display_name": "Taylor", **overrides}
    return await client.post(SIGNUP, json=payload, headers=await _csrf(client))


async def _login(client, email: str = "taylor@example.com", password: str = PASSWORD):
    return await client.post(
        LOGIN, json={"email": email, "password": password}, headers=await _csrf(client)
    )


# --- Lifecycle: signup → me → logout → login → me ---------------------------


async def test_signup_provisions_and_signs_in_in_one_step(client) -> None:
    response = await _signup(client)
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "taylor@example.com"
    assert body["display_name"] == "Taylor"

    # PRD story 1: the first session starts at signup — no separate login.
    me = await client.get(ME)
    assert me.status_code == 200
    assert me.json()["email"] == "taylor@example.com"


async def test_full_lifecycle_login_me_logout_me(client) -> None:
    await _signup(client)
    assert (await client.post(LOGOUT, headers=await _csrf(client))).status_code == 204
    assert (await client.get(ME)).status_code == 401

    assert (await _login(client)).status_code == 200
    assert (await client.get(ME)).status_code == 200


async def test_me_exposes_exactly_the_allowlisted_fields(client) -> None:
    await _signup(client)
    body = (await client.get(ME)).json()
    assert set(body) == {
        "id",
        "email",
        "display_name",
        "primary_currency",
        "email_verified",
        "created_at",
    }


async def test_no_response_ever_carries_password_material(client) -> None:
    signup = await _signup(client)
    login = await _login(client)
    me = await client.get(ME)
    for response in (signup, login, me):
        assert "password" not in response.text
        assert "argon2" not in response.text


async def test_me_without_a_session_is_401(client) -> None:
    assert (await client.get(ME)).status_code == 401


# --- Signup edges ------------------------------------------------------------


async def test_duplicate_email_signup_conflicts(client) -> None:
    await _signup(client)
    response = await _signup(client, display_name="Impostor")
    assert response.status_code == 409


async def test_signup_rejects_malformed_email_and_short_password(client) -> None:
    bad_email = await _signup(client, email="not-an-email")
    assert bad_email.status_code == 400
    short = await _signup(client, password="short")
    assert short.status_code == 400


async def test_display_name_defaults_to_the_email_local_part(client) -> None:
    response = await _signup(client, email="penny@example.com", display_name=None)
    assert response.status_code == 201
    assert response.json()["display_name"] == "penny"


async def test_signup_can_be_disabled_by_config(client, monkeypatch) -> None:
    # PRD story 11 (self-host): config, never a fork.
    monkeypatch.setattr(settings, "signup_enabled", False)
    response = await _signup(client)
    assert response.status_code == 403
    monkeypatch.setattr(settings, "signup_enabled", True)
    assert (await _signup(client)).status_code == 201


# --- Indistinguishable failures (PRD story 4) --------------------------------


async def test_wrong_password_and_unknown_email_are_indistinguishable(client) -> None:
    await _signup(client)
    await client.post(LOGOUT, headers=await _csrf(client))

    wrong_password = await _login(client, password="incorrect horse")
    unknown_email = await _login(client, email="nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    for response in (wrong_password, unknown_email):
        assert settings.session_cookie_name not in response.headers.get("set-cookie", "")


# --- Sessions on the wire -----------------------------------------------------


async def test_session_cookie_attributes_on_the_wire(client) -> None:
    response = await _signup(client)
    cookie = next(
        h
        for h in response.headers.get_list("set-cookie")
        if h.startswith(f"{settings.session_cookie_name}=")
    )
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert f"max-age={int(settings.session_absolute_ttl.total_seconds())}" in lowered
    assert "path=/" in lowered


async def test_logout_kills_the_session_server_side(client) -> None:
    await _signup(client)
    stolen = client.cookies[settings.session_cookie_name]
    await client.post(LOGOUT, headers=await _csrf(client))

    # PRD story 5: a stolen cookie dies with the server-side session.
    client.cookies.set(settings.session_cookie_name, stolen, domain="testserver.local")
    assert (await client.get(ME)).status_code == 401
    assert await Session.select().count() == 0


async def test_logout_without_a_session_is_idempotent(client) -> None:
    assert (await client.post(LOGOUT, headers=await _csrf(client))).status_code == 204


async def test_an_idle_expired_session_is_rejected_and_reaped(client) -> None:
    await _signup(client)
    session = (await Session.all())[0]
    session.last_seen_at = utcnow() - settings.session_idle_ttl - timedelta(seconds=1)
    await session.save()

    assert (await client.get(ME)).status_code == 401
    assert await Session.select().count() == 0


# --- CSRF ---------------------------------------------------------------------


async def test_mutations_without_a_csrf_token_are_rejected(client) -> None:
    response = await client.post(LOGIN, json={"email": "taylor@example.com", "password": PASSWORD})
    assert response.status_code == 403


# --- Rate limiting (PRD story 4) ----------------------------------------------


async def test_login_is_rate_limited_per_email(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_rate_limit_per_email", 3)
    await _signup(client)
    await client.post(LOGOUT, headers=await _csrf(client))

    for _ in range(3):
        assert (await _login(client, password="incorrect horse")).status_code == 401
    assert (await _login(client, password="incorrect horse")).status_code == 429
    # The right password is also locked out — the limit is on attempts.
    assert (await _login(client)).status_code == 429
    # A different account is unaffected by this email's limit.
    other = await _signup(client, email="penny@example.com")
    assert other.status_code == 201


async def test_login_is_rate_limited_per_ip_across_emails(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_rate_limit_per_ip", 3)
    for n in range(3):
        assert (await _login(client, email=f"probe{n}@example.com")).status_code == 401
    assert (await _login(client, email="probe99@example.com")).status_code == 429


async def test_signup_is_rate_limited_per_ip(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_rate_limit_per_ip", 2)
    assert (await _signup(client, email="a@example.com")).status_code == 201
    assert (await _signup(client, email="b@example.com")).status_code == 201
    assert (await _signup(client, email="c@example.com")).status_code == 429


async def test_rate_limit_windows_expire(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_rate_limit_per_email", 1)
    await _signup(client)
    await client.post(LOGOUT, headers=await _csrf(client))

    assert (await _login(client, password="incorrect horse")).status_code == 401
    assert (await _login(client)).status_code == 429

    aged = utcnow() - settings.auth_rate_limit_window - timedelta(seconds=1)
    for attempt in await AuthAttempt.all():
        attempt.created_at = aged
        await attempt.save()

    assert (await _login(client)).status_code == 200
