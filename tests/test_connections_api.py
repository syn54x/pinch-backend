"""M7 CP1 seam: connections over the public API (issue #33).

The provider seam is faked per test (PRD #31: CI never touches the
network); the keyless instance — no Plaid settings — is a first-class
citizen whose connection endpoints refuse cleanly while everything else
stands. Disconnect is absent: blocked on ferro-orm#325 (CP0 findings).
"""

import uuid

import pytest
from cryptography.fernet import Fernet

from pinch_backend import providers
from pinch_backend.crypto import decrypt_secret
from pinch_backend.models import Connection

CONNECTIONS = "/api/v1/connections"

PASSWORD = "correct horse battery staple"

CONNECTION_FIELDS = {
    "id",
    "provider",
    "status",
    "last_synced_at",
    "error_detail",
    "accounts",
    "created_at",
}


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


@pytest.fixture
def plaid_settings(monkeypatch):
    """An instance with Plaid configured (the fake provider answers for it)."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "test-client-id")
    monkeypatch.setattr(settings, "plaid_secret", "test-secret")
    monkeypatch.setattr(settings, "secret_encryption_key", Fernet.generate_key().decode())
    return settings


class FakeProvider:
    """Scriptable provider at the internal seam (PRD #31 testing decision)."""

    def __init__(self) -> None:
        self.accounts: list[providers.ProviderAccount] = []
        self.link_tokens_created: list[dict] = []
        self.exchanged: list[str] = []

    async def create_link_token(self, *, client_user_id: str) -> str:
        self.link_tokens_created.append({"client_user_id": client_user_id})
        return "link-sandbox-fake-token"

    async def exchange_public_token(self, public_token: str) -> providers.ExchangedToken:
        self.exchanged.append(public_token)
        return providers.ExchangedToken(
            access_token=f"access-fake-{public_token}", item_id=f"item-{public_token}"
        )

    async def get_accounts(self, access_token: str) -> list[providers.ProviderAccount]:
        return self.accounts


@pytest.fixture
def fake_provider(plaid_settings, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: fake)
    return fake


# --- keyless degradation -------------------------------------------------


async def test_keyless_link_token_refuses_cleanly(client, db) -> None:
    await _signup(client)
    response = await client.post(f"{CONNECTIONS}/link-token", headers=await _csrf(client))
    assert response.status_code == 403
    assert "not configured" in response.json()["detail"]


async def test_keyless_connection_create_refuses_cleanly(client, db) -> None:
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-x"}, headers=await _csrf(client)
    )
    assert response.status_code == 403
    assert "not configured" in response.json()["detail"]


async def test_keyless_list_answers_empty(client, db) -> None:
    """The health surface works keyless — it just has nothing to show."""
    await _signup(client)
    response = await client.get(CONNECTIONS)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


# --- the connect flow -----------------------------------------------------


def _script_accounts(fake: FakeProvider) -> None:
    fake.accounts = [
        providers.ProviderAccount(
            provider_account_id="plaid-checking",
            name="Everyday Checking",
            kind="depository",
            currency="USD",
        ),
        providers.ProviderAccount(
            provider_account_id="plaid-card",
            name="Rewards Card",
            kind="credit",
            currency="USD",
        ),
        providers.ProviderAccount(
            provider_account_id="plaid-mystery",
            name="Mystery Holding",
            kind="asset",  # the provider impl maps Plaid's `other` before the seam
            currency=None,
        ),
    ]


async def _connect(client, fake: FakeProvider) -> dict:
    _script_accounts(fake)
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_link_token_minted_for_acting_user(client, db, fake_provider) -> None:
    await _signup(client)
    response = await client.post(f"{CONNECTIONS}/link-token", headers=await _csrf(client))
    assert response.status_code == 201, response.text
    assert response.json() == {"link_token": "link-sandbox-fake-token"}
    assert len(fake_provider.link_tokens_created) == 1


async def test_connect_creates_connection_and_accounts(client, db, fake_provider) -> None:
    """One motion: exchange, Connection, one Account per consented account —
    no second selection layer (PRD #31)."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    assert set(body) == CONNECTION_FIELDS
    assert body["provider"] == "plaid"
    assert body["status"] == "active"
    assert body["last_synced_at"] is None
    labels = {a["label"]: a for a in body["accounts"]}
    assert set(labels) == {"Everyday Checking", "Rewards Card", "Mystery Holding"}
    assert labels["Everyday Checking"]["kind"] == "depository"
    assert labels["Rewards Card"]["kind"] == "credit"
    assert labels["Mystery Holding"]["kind"] == "asset"
    assert all(a["manual"] is False for a in labels.values())
    assert fake_provider.exchanged == ["public-abc"]


async def test_connect_currency_falls_back_to_primary(client, db, fake_provider) -> None:
    """Provider silence on currency answers with the acting user's primary
    currency, never a hardcoded default."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    mystery = next(a for a in body["accounts"] if a["label"] == "Mystery Holding")
    assert mystery["currency"] == "USD"  # signup default primary currency


async def test_access_token_encrypted_and_never_surfaced(client, db, fake_provider) -> None:
    """The Q4 invariant: write-only at the API surface, Fernet at rest."""
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-abc"}, headers=await _csrf(client)
    )
    assert "access-fake" not in response.text
    row = await Connection.where(lambda c: c.provider_item_id == "item-public-abc").first()
    assert row is not None
    assert row.encrypted_secret is not None
    assert b"access-fake" not in row.encrypted_secret
    assert decrypt_secret(row.encrypted_secret) == "access-fake-public-abc"


async def test_connected_accounts_appear_in_accounts_list(client, db, fake_provider) -> None:
    """Connected accounts are Accounts, full stop — the M4 surface shows
    them beside manual ones with manual=false."""
    await _signup(client)
    await _connect(client, fake_provider)
    response = await client.get("/api/v1/accounts")
    assert response.status_code == 200
    assert {a["label"] for a in response.json()["items"]} == {
        "Everyday Checking",
        "Rewards Card",
        "Mystery Holding",
    }


async def test_rejected_public_token_answers_400(client, db, fake_provider) -> None:
    """The recovery point: Plaid's code — and only the code — reaches the
    client, never an opaque 500."""

    async def refuse(public_token: str):
        raise providers.ProviderError("INVALID_PUBLIC_TOKEN", "expired")

    fake_provider.exchange_public_token = refuse
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-stale"}, headers=await _csrf(client)
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Plaid request failed: INVALID_PUBLIC_TOKEN"


async def test_provider_outage_answers_502(client, db, fake_provider) -> None:
    async def refuse(public_token: str):
        raise providers.ProviderError("INTERNAL_SERVER_ERROR", "plaid is down")

    fake_provider.exchange_public_token = refuse
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-x"}, headers=await _csrf(client)
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Plaid request failed: INTERNAL_SERVER_ERROR"


async def test_connection_detail_and_tenancy_404(client, db, fake_provider) -> None:
    await _signup(client)
    body = await _connect(client, fake_provider)
    detail = await client.get(f"{CONNECTIONS}/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == body["id"]

    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email="other@example.com")
    cross = await client.get(f"{CONNECTIONS}/{body['id']}")
    assert cross.status_code == 404
    assert (await client.get(f"{CONNECTIONS}/{uuid.uuid4()}")).status_code == 404
