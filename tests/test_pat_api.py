"""M3 CP3 seam: PATs over the public HTTP API (issue #10).

The full second-credential surface: mint with a cookie session, act with a
bearer header, die by row deletion. The probe routes stand in for every M4+
domain endpoint (the CP5 probe pattern): reaching them proves the guard
chain resolved identically for either credential; the write probe proves
scope enforcement before any real write endpoint exists.
"""

import re

import pytest
from litestar import get, post
from litestar.di import NamedDependency
from litestar.testing import AsyncTestClient

from pinch_backend.api.app import create_app
from pinch_backend.auth.models import PersonalAccessToken
from pinch_backend.models import Ledger
from pinch_backend.settings import settings

SIGNUP = "/api/v1/auth/signup"
ME = "/api/v1/auth/me"
PATS = "/api/v1/auth/pats"

PASSWORD = "correct horse battery staple"
PAT_RE = re.compile(r"pinch_pat_[A-Za-z0-9_-]{43}")


@get("/_probe/ledger")
async def ledger_probe(current_ledger: NamedDependency[Ledger]) -> dict[str, str]:
    return {"ledger_id": str(current_ledger.id)}


@post("/_probe/write")
async def write_probe(current_ledger: NamedDependency[Ledger]) -> dict[str, str]:
    """Test-only stand-in for M4's first write endpoint: reaching it means
    the credential resolved AND carries write scope."""
    return {"ledger_id": str(current_ledger.id)}


@pytest.fixture
async def probe_client(db):
    app = create_app(manage_database=False)
    app.register(ledger_probe)
    app.register(write_probe)
    async with AsyncTestClient(app, base_url="https://testserver.local") as c:
        yield c


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com"):
    return await client.post(
        SIGNUP,
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )


async def _mint(
    client, name: str = "ci-script", scopes: list[str] | None = None
) -> tuple[dict, str]:
    response = await client.post(
        PATS,
        json={"name": name, "scopes": scopes or ["read", "write"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body, body["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Create: the secret appears exactly once (story 1) --------------------------


async def test_create_shows_the_secret_exactly_once_and_never_again(client) -> None:
    await _signup(client)
    body, token = await _mint(client)

    assert PAT_RE.fullmatch(token)
    assert token.startswith(body["display_prefix"])
    assert body["name"] == "ci-script"
    assert body["scopes"] == ["read", "write"]

    # The list never carries the secret in any form — allowlisted fields only.
    listed = (await client.get(PATS)).json()
    (item,) = listed["items"]
    assert set(item) == {"id", "name", "scopes", "display_prefix", "created_at", "last_used_at"}
    assert token not in (await client.get(PATS)).text


async def test_write_implies_read_in_the_granted_scopes(client) -> None:
    await _signup(client)
    body, _ = await _mint(client, scopes=["write"])
    assert body["scopes"] == ["read", "write"]

    read_only, _ = await _mint(client, name="reader", scopes=["read"])
    assert read_only["scopes"] == ["read"]


async def test_create_validates_name_and_scopes(client) -> None:
    await _signup(client)
    headers = await _csrf(client)
    for bad in (
        {"name": "", "scopes": ["read"]},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["admin"]},
        {"scopes": ["read"]},
    ):
        assert (await client.post(PATS, json=bad, headers=headers)).status_code == 400


# --- The bearer acts as the user (stories 3, and 2's chain parity) ---------------


async def test_bearer_and_cookie_get_identical_answers_from_me(client) -> None:
    await _signup(client)
    _, token = await _mint(client)
    via_cookie = (await client.get(ME)).json()

    client.cookies.clear()
    via_bearer_response = await client.get(ME, headers=_bearer(token))
    assert via_bearer_response.status_code == 200
    assert via_bearer_response.json() == via_cookie


async def test_bearer_reaches_the_ledger_through_the_same_chain(probe_client) -> None:
    await _signup(probe_client)
    via_cookie = (await probe_client.get("/_probe/ledger")).json()
    _, token = await _mint(probe_client)

    probe_client.cookies.clear()
    via_bearer = await probe_client.get("/_probe/ledger", headers=_bearer(token))
    assert via_bearer.status_code == 200
    assert via_bearer.json() == via_cookie


async def test_bearer_passes_the_same_verification_gate(probe_client, monkeypatch) -> None:
    """The hosted email-verification gate (AGENTS I-2) must treat both
    credentials identically: it lives in the ledger dependency, past the
    point where the credentials have merged."""
    monkeypatch.setattr(settings, "verification_required", True)
    await _signup(probe_client)
    _, token = await _mint(probe_client)

    assert (await probe_client.get(ME, headers=_bearer(token))).status_code == 200
    assert (await probe_client.get("/_probe/ledger", headers=_bearer(token))).status_code == 403
    assert (await probe_client.get("/_probe/ledger")).status_code == 403


async def test_last_used_at_is_touched_by_bearer_use(client) -> None:
    await _signup(client)
    _, token = await _mint(client)
    (before,) = (await client.get(PATS)).json()["items"]
    assert before["last_used_at"] is None

    await client.get(ME, headers=_bearer(token))
    (after,) = (await client.get(PATS)).json()["items"]
    assert after["last_used_at"] is not None


# --- Lifecycle: revocation is immediate death (story 2) --------------------------


async def test_revoke_kills_the_bearer_immediately(client) -> None:
    await _signup(client)
    body, token = await _mint(client)
    assert (await client.get(ME, headers=_bearer(token))).status_code == 200

    revoke = await client.delete(PATS + f"/{body['id']}", headers=await _csrf(client))
    assert revoke.status_code == 204
    assert (await client.get(ME, headers=_bearer(token))).status_code == 401
    assert await PersonalAccessToken.select().count() == 0


async def test_revoking_anothers_pat_is_404_and_leaves_it_alive(client) -> None:
    await _signup(client)
    _, token = await _mint(client)
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))

    await _signup(client, email="other@example.com")
    other_pat_id = (await client.get(PATS)).json()["items"] or None
    assert other_pat_id is None  # sanity: the second user starts with none

    victim = (await PersonalAccessToken.all())[0]
    response = await client.delete(PATS + f"/{victim.id}", headers=await _csrf(client))
    # 404, not 403: the response must not confirm the PAT exists.
    assert response.status_code == 404
    assert (await client.get(ME, headers=_bearer(token))).status_code == 200


# --- The management fence (story 5) ----------------------------------------------


async def test_a_pat_can_never_manage_pats(client) -> None:
    await _signup(client)
    body, token = await _mint(client)  # full write scope

    client.cookies.clear()
    headers = _bearer(token)
    assert (
        await client.post(PATS, json={"name": "sneaky", "scopes": ["write"]}, headers=headers)
    ).status_code == 401
    assert (await client.get(PATS, headers=headers)).status_code == 401
    assert (await client.delete(PATS + f"/{body['id']}", headers=headers)).status_code == 401
    # The fence didn't kill the token's legitimate powers.
    assert (await client.get(ME, headers=headers)).status_code == 200


async def test_pat_management_without_any_credential_is_401(client) -> None:
    assert (await client.get(PATS)).status_code == 401
    assert (
        await client.post(PATS, json={"name": "x", "scopes": ["read"]}, headers=await _csrf(client))
    ).status_code == 401


# --- Scope enforcement (story 4) --------------------------------------------------


async def test_read_tokens_are_refused_by_write_endpoints_with_403(probe_client) -> None:
    await _signup(probe_client)
    _, read_token = await _mint(probe_client, name="reader", scopes=["read"])
    _, write_token = await _mint(probe_client, name="writer", scopes=["write"])
    probe_client.cookies.clear()

    assert (
        await probe_client.get("/_probe/ledger", headers=_bearer(read_token))
    ).status_code == 200
    refused = await probe_client.post("/_probe/write", headers=_bearer(read_token))
    assert refused.status_code == 403

    allowed = await probe_client.post("/_probe/write", headers=_bearer(write_token))
    assert allowed.status_code == 201


# --- The CSRF matrix (story 3 / M2 story 14) ---------------------------------------


async def test_csrf_matrix_bearer_exempt_cookie_protected(probe_client) -> None:
    """Header auth isn't CSRF-able, so bearer mutations need no token;
    cookie mutations keep the full double-submit ceremony."""
    await _signup(probe_client)
    _, token = await _mint(probe_client)

    # Cookie without the CSRF header: refused, exactly as M2 left it.
    assert (await probe_client.post("/_probe/write")).status_code == 403
    # Cookie with the header: accepted.
    assert (
        await probe_client.post("/_probe/write", headers=await _csrf(probe_client))
    ).status_code == 201

    # Bearer with no CSRF material at all: accepted by construction.
    probe_client.cookies.clear()
    assert (await probe_client.post("/_probe/write", headers=_bearer(token))).status_code == 201


# --- Failure shapes and precedence -------------------------------------------------


async def test_an_unknown_bearer_fails_exactly_like_a_missing_cookie(client) -> None:
    await _signup(client)
    client.cookies.clear()

    no_credential = await client.get(ME)
    bad_bearer = await client.get(ME, headers=_bearer("pinch_pat_" + "A" * 43))
    assert no_credential.status_code == bad_bearer.status_code == 401
    assert no_credential.json() == bad_bearer.json()


async def test_a_malformed_authorization_header_is_401(client) -> None:
    await _signup(client)
    client.cookies.clear()
    for header in ("Bearer", "Bearer ", "Basic dXNlcjpwYXNz"):
        response = await client.get(ME, headers={"Authorization": header})
        assert response.status_code == 401, f"Authorization: {header!r}"


async def test_a_bearer_header_wins_over_a_valid_cookie_and_fails_closed(client) -> None:
    """The invariant that makes the CSRF exemption sound: when a bearer
    header is present, the cookie is never the acting credential — an
    invalid bearer fails the request even though a valid session rides
    along in the cookie jar."""
    await _signup(client)
    assert (await client.get(ME)).status_code == 200  # the cookie is valid
    assert (await client.get(ME, headers=_bearer("pinch_pat_" + "A" * 43))).status_code == 401


async def test_failed_bearers_are_rate_limited_per_ip(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_rate_limit_per_ip", 3)
    await _signup(client)
    _, token = await _mint(client)
    client.cookies.clear()

    for _ in range(3):
        assert (await client.get(ME, headers=_bearer("pinch_pat_" + "B" * 43))).status_code == 401
    assert (await client.get(ME, headers=_bearer("pinch_pat_" + "B" * 43))).status_code == 429
    # Only failures count: the real token is unaffected by the limiter.
    assert (await client.get(ME, headers=_bearer(token))).status_code == 200


# --- The list is born onto the pagination convention (story 8 / issue #9) -----------


async def test_pat_list_paginates_on_the_convention(client) -> None:
    await _signup(client)
    for n in range(3):
        await _mint(client, name=f"pat-{n}")

    first = (await client.get(PATS, params={"limit": 2})).json()
    assert len(first["items"]) == 2
    rest = (await client.get(PATS, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None

    ids = [item["id"] for item in first["items"] + rest["items"]]
    assert ids == sorted(ids)
    assert (await client.get(PATS, params={"cursor": "junk"})).status_code == 400
