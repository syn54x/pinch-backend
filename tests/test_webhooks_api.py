"""M11 CP0 seam: the doorbell receiver at the HTTP boundary (issue #78;
PRD #77; ADR 0008).

Real cryptography, no bypass: tests mint a P-256 keypair, fake the
provider's verification-key method to serve the matching JWK, and POST
genuinely ES256-signed payloads. The doorbell-only law is proven
behaviorally — a verified ring enqueues exactly the job a clicked Refresh
would, and everything else (forgeries, unknown codes, unmatched items)
enqueues nothing.
"""

import base64
import hashlib
import json
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from pinch_backend import providers
from test_investments_api import FakeInvestmentsProvider

WEBHOOK = "/webhooks/plaid"
CONNECTIONS = "/api/v1/connections"

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def _connect(client) -> dict:
    """The fake's completion mints item_id ``item-public-abc`` for this
    token — the provider_item_id webhooks ring with."""
    response = await client.post(
        CONNECTIONS, json={"provider": "plaid", "token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Real ES256 material ---------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_keypair():
    """A fresh P-256 keypair per test, with a unique kid so the receiver's
    per-kid key cache can never bleed one test's key into another."""
    private = ec.generate_private_key(ec.SECP256R1())
    kid = f"kid-{uuid.uuid4().hex}"
    numbers = private.public_key().public_numbers()
    jwk = {
        "alg": "ES256",
        "crv": "P-256",
        "kid": kid,
        "kty": "EC",
        "use": "sig",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }
    return private, kid, jwk


def _sign(
    private,
    kid: str,
    body: bytes,
    *,
    alg: str = "ES256",
    iat: int | None = None,
    body_hash: str | None = None,
) -> str:
    """A genuinely signed Plaid-Verification JWT: ES256 over the compact
    signing input, iat and request_body_sha256 claims as Plaid documents
    them. The knobs exist so tests can produce precisely-wrong tokens."""
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    claims = {
        "iat": int(time.time()) if iat is None else iat,
        "request_body_sha256": body_hash or hashlib.sha256(body).hexdigest(),
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    der = private.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{_b64url(raw)}"


def _doorbell(webhook_type: str, webhook_code: str, item_id: str, **extra) -> bytes:
    return json.dumps(
        {
            "webhook_type": webhook_type,
            "webhook_code": webhook_code,
            "item_id": item_id,
            "environment": "sandbox",
            **extra,
        }
    ).encode()


async def _ring(client, token: str | None, body: bytes):
    """POST like Plaid does: raw JSON body, the JWT in Plaid-Verification,
    no session, no CSRF header."""
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["plaid-verification"] = token
    return await client.post(WEBHOOK, content=body, headers=headers)


# --- The provider fake, grown a key endpoint -------------------------------------


class FakeWebhookProvider(FakeInvestmentsProvider):
    """The investments fake grown the verification-key method: serves the
    JWKs it was minted with, counts fetches so the per-kid cache is
    observable behavior."""

    def __init__(self, jwks: dict[str, dict]) -> None:
        super().__init__()
        self.jwks = jwks
        self.key_fetches: list[str] = []

    async def get_webhook_verification_key(self, key_id: str) -> dict:
        self.key_fetches.append(key_id)
        key = self.jwks.get(key_id)
        if key is None:
            raise providers.ProviderError(code="INVALID_KEY_ID", message="no such key")
        return key


@pytest.fixture
def keypair():
    return _mint_keypair()


@pytest.fixture
def fake_provider(keypair, monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "test-client-id")
    monkeypatch.setattr(settings, "plaid_secret", "test-secret")
    monkeypatch.setattr(settings, "secret_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "plaid_webhook_url", "https://pinch.example/webhooks/plaid")
    _, kid, jwk = keypair
    fake = FakeWebhookProvider({kid: jwk})
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


@pytest.fixture
async def connection(client, db, fake_provider, job_connector) -> dict:
    """A connected Item with provider_item_id ``item-public-abc``, the job
    queue drained of the connect flow's own enqueues."""
    await _signup(client)
    body = await _connect(client)
    job_connector.reset()
    return body


def _queued(job_connector) -> list[dict]:
    return list(job_connector.jobs.values())


# --- Verified doorbells dispatch -------------------------------------------------


async def test_signed_transactions_doorbell_enqueues_the_connection_sync(
    client, connection, keypair, job_connector
) -> None:
    """The tracer: Plaid rings TRANSACTIONS, Pinch runs the same sync a
    clicked Refresh would — same job, same per-connection lock — and 200s."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    jobs = _queued(job_connector)
    assert [j["task_name"] for j in jobs] == ["sync.sync_connection"]
    assert jobs[0]["args"] == {"connection_id": connection["id"]}
    assert jobs[0]["lock"] == f"sync:{connection['id']}"


@pytest.mark.parametrize(
    ("webhook_type", "webhook_code"),
    [
        ("HOLDINGS", "DEFAULT_UPDATE"),
        ("INVESTMENTS_TRANSACTIONS", "DEFAULT_UPDATE"),
        ("INVESTMENTS_TRANSACTIONS", "HISTORICAL_UPDATE"),
    ],
)
async def test_investments_doorbells_enqueue_the_investments_job(
    client, connection, keypair, job_connector, webhook_type, webhook_code
) -> None:
    """Price-change rings (near-daily on market days) and activity updates
    both land on the existing investments job, same per-connection lock."""
    private, kid, _ = keypair
    body = _doorbell(webhook_type, webhook_code, "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    jobs = _queued(job_connector)
    assert [j["task_name"] for j in jobs] == ["sync.sync_investments"]
    assert jobs[0]["args"] == {"connection_id": connection["id"]}
    assert jobs[0]["lock"] == f"sync:{connection['id']}"


async def test_the_signing_key_is_fetched_once_per_kid(
    client, connection, keypair, fake_provider, job_connector
) -> None:
    """The per-kid cache is behavior: two rings, one key fetch."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    assert (await _ring(client, _sign(private, kid, body), body)).status_code == 200
    assert (await _ring(client, _sign(private, kid, body), body)).status_code == 200
    assert fake_provider.key_fetches == [kid]
    assert len(_queued(job_connector)) == 2  # duplicates enqueue; the lock serializes


# --- Forgeries answer 401 and enqueue nothing ------------------------------------


async def test_a_signature_from_the_wrong_key_answers_401(
    client, connection, keypair, job_connector
) -> None:
    """Signed by a key Plaid never served for that kid — the forged-
    doorbell shape (PRD #77 story 10)."""
    _, kid, _ = keypair
    imposter = ec.generate_private_key(ec.SECP256R1())
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    response = await _ring(client, _sign(imposter, kid, body), body)

    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_a_non_es256_algorithm_answers_401(
    client, connection, keypair, job_connector
) -> None:
    """alg is required to be ES256 before any key is consulted — no
    downgrade, no alg=none family of holes."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    for alg in ("none", "HS256"):
        response = await _ring(client, _sign(private, kid, body, alg=alg), body)
        assert response.status_code == 401, alg
    assert _queued(job_connector) == []


async def test_a_stale_iat_answers_401(client, connection, keypair, job_connector) -> None:
    """A genuinely signed token older than the freshness window is a
    replay, not a doorbell."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body, iat=int(time.time()) - 600), body)

    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_a_body_hash_mismatch_answers_401(client, connection, keypair, job_connector) -> None:
    """A valid token stapled onto a different body is a swap, not a ring:
    the request_body_sha256 claim must match the raw bytes received."""
    private, kid, _ = keypair
    signed_body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")
    swapped_body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-other")

    response = await _ring(client, _sign(private, kid, signed_body), swapped_body)

    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_a_missing_verification_header_answers_401(client, connection, job_connector) -> None:
    response = await _ring(
        client, None, _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")
    )
    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_a_keyless_instance_answers_401(client, db, keypair, job_connector) -> None:
    """No bypass in any configuration includes the keyless one: without
    Plaid there is no key to verify against, so every ring is a 401 —
    never an unverified dispatch."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 401
    assert _queued(job_connector) == []


async def test_a_ring_carries_no_session_and_no_cookies(
    client, connection, keypair, job_connector
) -> None:
    """Plaid is not a browser: the sessionless, cookie-less POST — exactly
    what production receives — verifies and dispatches."""
    private, kid, _ = keypair
    client.cookies.clear()
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    assert [j["task_name"] for j in _queued(job_connector)] == ["sync.sync_connection"]


# --- Verified but not ours: log-and-200 ------------------------------------------


async def test_an_unknown_webhook_code_answers_200_and_enqueues_nothing(
    client, connection, keypair, job_connector
) -> None:
    """Codes outside the doorbell table are acknowledged, not errored — a
    non-200 only makes Plaid retry something we chose to ignore."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "RECURRING_TRANSACTIONS_UPDATE", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200
    assert _queued(job_connector) == []


async def test_an_unmatched_item_id_answers_200_and_enqueues_nothing(
    client, connection, keypair, job_connector
) -> None:
    """A ring for an Item we no longer hold: acknowledged, never confirmed
    — the response tells a prober nothing about which item ids exist."""
    private, kid, _ = keypair
    body = _doorbell("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "item-unknown")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200
    assert _queued(job_connector) == []


# --- ITEM lifecycle (M11 CP2, issue #80): prompt health, no new states -----------


async def _connection_view(client, connection_id: str) -> dict:
    response = await client.get(f"{CONNECTIONS}/{connection_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_item_error_with_an_auth_code_flips_to_reauth_required(
    client, connection, keypair, job_connector
) -> None:
    """The moment Plaid learns a login died, so do we (PRD #77 story 3):
    ITEM ERROR maps through the sync engine's own auth-code partition —
    same status, same error_detail shape a failed sync would write."""
    private, kid, _ = keypair
    body = _doorbell(
        "ITEM", "ERROR", "item-public-abc", error={"error_code": "ITEM_LOGIN_REQUIRED"}
    )

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "reauth_required"
    assert view["error_detail"] == "ITEM_LOGIN_REQUIRED"
    assert _queued(job_connector) == []  # a dead login is repair territory, not a sync


async def test_item_error_with_a_non_auth_code_lands_as_error(
    client, connection, keypair, job_connector
) -> None:
    private, kid, _ = keypair
    body = _doorbell(
        "ITEM",
        "ERROR",
        "item-public-abc",
        error={"error_code": "INSTITUTION_NO_LONGER_SUPPORTED"},
    )

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "error"
    assert view["error_detail"] == "INSTITUTION_NO_LONGER_SUPPORTED"
    assert _queued(job_connector) == []


async def test_login_repaired_clears_reauth_and_enqueues_the_proving_sync(
    client, connection, keypair, job_connector
) -> None:
    """Self-healed without update-mode (PRD #77 story 4): the broken badge
    clears and a sync runs to prove the repair — the webhook writes only
    the statuses; last_synced_at stays the sync's own stamp."""
    private, kid, _ = keypair
    broken = _doorbell(
        "ITEM", "ERROR", "item-public-abc", error={"error_code": "ITEM_LOGIN_REQUIRED"}
    )
    assert (await _ring(client, _sign(private, kid, broken), broken)).status_code == 200
    job_connector.reset()

    repaired = _doorbell("ITEM", "LOGIN_REPAIRED", "item-public-abc")
    response = await _ring(client, _sign(private, kid, repaired), repaired)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "active"
    assert view["error_detail"] is None
    jobs = _queued(job_connector)
    assert [j["task_name"] for j in jobs] == ["sync.sync_connection"]
    assert jobs[0]["lock"] == f"sync:{connection['id']}"


async def test_user_permission_revoked_lands_as_error_with_the_code(
    client, connection, keypair, job_connector
) -> None:
    """Honestly broken beats silently stale (PRD #77 story 5)."""
    private, kid, _ = keypair
    body = _doorbell("ITEM", "USER_PERMISSION_REVOKED", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "error"
    assert view["error_detail"] == "USER_PERMISSION_REVOKED"
    assert _queued(job_connector) == []


@pytest.mark.parametrize(
    "webhook_code",
    ["PENDING_DISCONNECT", "PENDING_EXPIRATION", "WEBHOOK_UPDATE_ACKNOWLEDGED"],
)
async def test_log_only_item_events_change_nothing(
    client, connection, keypair, job_connector, webhook_code
) -> None:
    """Advance warnings and acknowledgements are logged and nothing more
    (PRD #77 decision 5): no state, no jobs — the eventual ERROR is the
    acting event."""
    private, kid, _ = keypair
    body = _doorbell("ITEM", webhook_code, "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "active"
    assert view["error_detail"] is None
    assert _queued(job_connector) == []


async def test_item_error_without_an_error_object_still_lands_as_error(
    client, connection, keypair, job_connector
) -> None:
    """A malformed ERROR ring degrades to the generic broken state — never
    a crash, never a silent active."""
    private, kid, _ = keypair
    body = _doorbell("ITEM", "ERROR", "item-public-abc")

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "error"
    assert view["error_detail"] == "ITEM_ERROR"
    assert _queued(job_connector) == []


async def test_an_item_event_for_an_unmatched_item_changes_nothing(
    client, connection, keypair, job_connector
) -> None:
    """CP0's unmatched posture holds for the new family: log-and-200,
    and the connection we do hold is untouched."""
    private, kid, _ = keypair
    body = _doorbell("ITEM", "ERROR", "item-unknown", error={"error_code": "ITEM_LOGIN_REQUIRED"})

    response = await _ring(client, _sign(private, kid, body), body)

    assert response.status_code == 200, response.text
    view = await _connection_view(client, connection["id"])
    assert view["status"] == "active"
    assert _queued(job_connector) == []


def test_no_new_connection_statuses_exist() -> None:
    """The no-new-states law (PRD #77 decision 5): webhooks change when we
    learn, never what states exist."""
    from pinch_backend.models import ConnectionStatus

    assert {s.value for s in ConnectionStatus} == {"active", "error", "reauth_required"}


# --- The contract seam does not move ---------------------------------------------


async def test_the_webhook_route_is_absent_from_the_openapi_document(client, db) -> None:
    """Provider plumbing, not Developer API (ADR 0008): the generated
    frontend client must never learn this route exists."""
    schema = (await client.get("/api/v1/schema/openapi.json")).json()
    assert not any("webhook" in path for path in schema["paths"])
