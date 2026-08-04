"""M7 CP1 seam: connections over the public API (issue #33; provider-
neutral sweep M13 CP1, #88).

The provider seam is faked per test at ``get_provider`` (PRD #31: CI
never touches the network); the keyless instance — no provider settings —
is a first-class citizen whose provider-touching endpoints refuse cleanly
while everything else stands. Configuration is per provider (PRD #86
story 16): the catalog endpoint reports it, and each provider's refusal
names the provider.
"""

import uuid

import pytest
from cryptography.fernet import Fernet
from ferro import UniqueViolationError

from pinch_backend import providers
from pinch_backend.crypto import decrypt_secret
from pinch_backend.models import Connection, ConnectionProvider, Enrollment, Ledger

CONNECTIONS = "/api/v1/connections"

PASSWORD = "correct horse battery staple"

CONNECTION_FIELDS = {
    "id",
    "institution_name",
    "provider",
    "provider_institution_id",
    "status",
    "last_synced_at",
    "error_detail",
    "investments_error_detail",
    "investments_consent_required",
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
    """Scriptable provider at the registry seam (PRD #31 testing
    decision). ``materialize`` stands in for ``get_provider`` (M13 CP1):
    it records which provider was asked for and which secret bound the
    instance — the credential assertions that method signatures no longer
    carry."""

    def __init__(self) -> None:
        self.accounts: list[providers.ProviderAccount] = []
        self.sessions_created: list[dict] = []
        self.completed: list[str] = []
        self.removed: list[str | None] = []
        self.institution_id: str | None = "ins_platypus"
        self.institution_name: str | None = "First Platypus Bank"
        self.materialized: list[dict] = []
        self.secret: str | None = None
        """The most recent materialization's bound credential."""

    def materialize(self, provider, *, secret: str | None = None) -> "FakeProvider":
        self.materialized.append({"provider": provider, "secret": secret})
        self.secret = secret
        return self

    async def get_item_state(self) -> providers.ItemState:
        """The midnight-UTC reconcile cron (M11) can fire inside any test's
        worker window. Answer with the registered URL and no update stamps
        so the verdict is "quiet" — a no-op pass instead of an
        AttributeError'd job failing an unrelated test's assertions."""
        from pinch_backend.settings import settings

        return providers.ItemState(webhook=settings.plaid_webhook_url)

    async def update_webhook(self, url: str) -> None:
        return None

    async def get_institution(self) -> providers.ProviderInstitution:
        return providers.ProviderInstitution(
            provider_institution_id=self.institution_id, name=self.institution_name
        )

    async def remove_item(self) -> None:
        self.removed.append(self.secret)

    async def create_connect_session(self, *, client_user_id: str) -> str:
        self.sessions_created.append({"client_user_id": client_user_id, "secret": self.secret})
        return "link-sandbox-fake-token"

    async def complete_connect(self, token: str) -> providers.ConnectResult:
        self.completed.append(token)
        self.secret = f"access-fake-{token}"
        return providers.ConnectResult(
            provider_item_id=f"item-{token}",
            provider_institution_id=self.institution_id,
            institution_name=self.institution_name,
            secret=self.secret,
        )

    async def get_accounts(self) -> list[providers.ProviderAccount]:
        return self.accounts


@pytest.fixture
def fake_provider(plaid_settings, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


# --- keyless degradation -------------------------------------------------


async def test_keyless_connect_session_refuses_cleanly(client, db) -> None:
    await _signup(client)
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "plaid"},
        headers=await _csrf(client),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Plaid is not configured on this instance"


async def test_keyless_connection_create_refuses_cleanly(client, db) -> None:
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"provider": "plaid", "token": "public-x"}, headers=await _csrf(client)
    )
    assert response.status_code == 403
    assert "not configured" in response.json()["detail"]


async def test_keyless_refresh_of_nothing_is_404(client, db) -> None:
    """The refusal is per the connection's provider (M13 CP1), so a
    connection that doesn't exist answers 404 — there is nothing whose
    provider could refuse."""
    await _signup(client)
    response = await client.post(f"{CONNECTIONS}/{uuid.uuid4()}/sync", headers=await _csrf(client))
    assert response.status_code == 404


async def test_dekeyed_refresh_refuses_per_provider(client, db, fake_provider, monkeypatch) -> None:
    """An instance whose Plaid keys were removed after a connect refuses
    that connection's refresh, naming the provider."""
    from pinch_backend.settings import settings

    await _signup(client)
    body = await _connect(client, fake_provider)
    monkeypatch.setattr(settings, "plaid_client_id", "")
    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 403
    assert response.json()["detail"] == "Plaid is not configured on this instance"


async def test_unconfigured_mx_refuses_even_when_plaid_is_configured(
    client, db, fake_provider
) -> None:
    """Partial configuration is a valid state (PRD #86 story 16): each
    provider refuses independently, naming itself."""
    await _signup(client)
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "mx"},
        headers=await _csrf(client),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "MX is not configured on this instance"


async def test_keyless_list_answers_empty(client, db) -> None:
    """The health surface works keyless — it just has nothing to show."""
    await _signup(client)
    response = await client.get(CONNECTIONS)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


# --- the provider catalog (M13 CP1) ---------------------------------------


async def test_catalog_lists_every_known_provider_keyless(client, db) -> None:
    """One entry per known provider, configured or not — the picker's
    honest surface. Keyless: nothing is configured, capabilities stand."""
    await _signup(client)
    response = await client.get(f"{CONNECTIONS}/providers")
    assert response.status_code == 200
    entries = {e["provider"]: e for e in response.json()}
    assert set(entries) == {"plaid", "mx"}
    assert entries["plaid"]["configured"] is False
    assert entries["mx"]["configured"] is False
    assert entries["plaid"]["capabilities"] == ["transactions", "balances", "holdings", "activity"]
    assert entries["mx"]["capabilities"] == ["transactions", "balances"]


async def test_catalog_reports_per_provider_configuration(client, db, plaid_settings) -> None:
    """Plaid configured flips exactly Plaid's entry; MX stays honest
    about its missing implementation (CP2, #89)."""
    await _signup(client)
    entries = {e["provider"]: e for e in (await client.get(f"{CONNECTIONS}/providers")).json()}
    assert entries["plaid"]["configured"] is True
    assert entries["mx"]["configured"] is False


# --- the connect flow -----------------------------------------------------


def _script_accounts(fake: FakeProvider) -> None:
    fake.accounts = [
        providers.ProviderAccount(
            provider_account_id="plaid-checking",
            name="Everyday Checking",
            kind="depository",
            currency="USD",
            mask="4821",
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
        CONNECTIONS, json={"provider": "plaid", "token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_connect_session_minted_for_acting_user(client, db, fake_provider) -> None:
    """The session answers {provider, token}: the opaque string the
    chosen provider's widget consumes."""
    await _signup(client)
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "plaid"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"provider": "plaid", "token": "link-sandbox-fake-token"}
    assert len(fake_provider.sessions_created) == 1
    # A fresh connect mints an unbound session — no connection credential.
    assert fake_provider.sessions_created[0]["secret"] is None


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
    assert fake_provider.completed == ["public-abc"]


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
        CONNECTIONS, json={"provider": "plaid", "token": "public-abc"}, headers=await _csrf(client)
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


async def test_rejected_completion_token_answers_400(client, db, fake_provider) -> None:
    """The recovery point: the provider's code — and only the code —
    reaches the client, naming the provider, never an opaque 500."""

    async def refuse(token: str):
        raise providers.ProviderError("INVALID_PUBLIC_TOKEN", "expired")

    fake_provider.complete_connect = refuse
    await _signup(client)
    response = await client.post(
        CONNECTIONS,
        json={"provider": "plaid", "token": "public-stale"},
        headers=await _csrf(client),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Plaid request failed: INVALID_PUBLIC_TOKEN"


async def test_provider_outage_answers_502(client, db, fake_provider) -> None:
    async def refuse(token: str):
        raise providers.ProviderError("INTERNAL_SERVER_ERROR", "plaid is down")

    fake_provider.complete_connect = refuse
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"provider": "plaid", "token": "public-x"}, headers=await _csrf(client)
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Plaid request failed: INTERNAL_SERVER_ERROR"


# --- disconnect: severs, never destroys (unblocked by ferro-orm#325) ------


async def test_disconnect_severs_but_keeps_accounts(client, db, fake_provider) -> None:
    """CONTEXT.md: disconnecting severs the link, never the data — the
    accounts live on as manual accounts, history intact."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    response = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert response.status_code == 204, response.text
    # Plaid's side revoked with the decrypted token, never a guess
    assert fake_provider.removed == ["access-fake-public-abc"]
    # The connection is gone; the accounts stand, structurally manual now
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).status_code == 404
    accounts = (await client.get("/api/v1/accounts")).json()["items"]
    assert {a["label"] for a in accounts} == {
        "Everyday Checking",
        "Rewards Card",
        "Mystery Holding",
    }
    assert all(a["manual"] is True for a in accounts)


async def test_disconnected_account_accepts_manual_entries(client, db, fake_provider) -> None:
    """The M4 machinery lights up for a severed account automatically."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    account_id = body["accounts"][0]["id"]
    await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    response = await client.post(
        f"/api/v1/accounts/{account_id}/balance-entries",
        json={"amount_minor": 123_45},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


async def test_disconnect_provider_outage_severs_nothing(client, db, fake_provider) -> None:
    """Half-severed is worse than not severed: if Plaid's revocation fails,
    the connection remains and the client retries."""

    async def refuse() -> None:
        raise providers.ProviderError("INTERNAL_SERVER_ERROR", "plaid is down")

    fake_provider.remove_item = refuse
    await _signup(client)
    body = await _connect(client, fake_provider)
    response = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert response.status_code == 502
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).status_code == 200


async def test_disconnect_item_already_gone_still_severs(client, db, fake_provider) -> None:
    """Plaid not knowing the item anymore is success, not failure — the
    endpoint is idempotent from the client's seat."""

    async def already_gone() -> None:
        raise providers.ProviderError("ITEM_NOT_FOUND", "no such item")

    fake_provider.remove_item = already_gone
    await _signup(client)
    body = await _connect(client, fake_provider)
    response = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert response.status_code == 204
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).status_code == 404


async def test_disconnect_tenancy_404(client, db, fake_provider) -> None:
    await _signup(client)
    body = await _connect(client, fake_provider)
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email="other@example.com")
    response = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert response.status_code == 404


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


async def test_connect_captures_institution_identity_and_mask(client, db, fake_provider) -> None:
    """The humane surface (F2 enabler, #39) plus the dupe guard's basis
    (M13 CP1): bank names AND the provider's institution id — captured
    server-side at complete_connect, never client-trusted."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    assert body["institution_name"] == "First Platypus Bank"
    assert body["provider_institution_id"] == "ins_platypus"
    labels = {a["label"]: a for a in body["accounts"]}
    assert labels["Everyday Checking"]["mask"] == "4821"
    assert labels["Rewards Card"]["mask"] is None


async def test_connect_survives_a_missing_institution_identity(client, db, fake_provider) -> None:
    """Institution identity is a nicety — the provider not answering one
    (failed lookup, institution-less Item) must never block a connect."""
    fake_provider.institution_id = None
    fake_provider.institution_name = None
    await _signup(client)
    body = await _connect(client, fake_provider)
    assert body["status"] == "active"
    assert body["institution_name"] is None
    assert body["provider_institution_id"] is None


async def test_repair_session_provider_must_match_the_connection(
    client, db, fake_provider, monkeypatch
) -> None:
    """A repair session is the connection's provider's to mint: asking
    another provider for it is a client bug, answered 400 — never a
    silent re-route (M13 CP1)."""
    monkeypatch.setattr(providers, "provider_configured", lambda provider: True)
    await _signup(client)
    body = await _connect(client, fake_provider)
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "mx", "connection_id": body["id"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Provider does not match the connection"


# --- MX connect end-to-end (M13 CP2, issue #89) ----------------------------


class FakeMXProvider:
    """Scriptable MX at the registry seam: guid bindings instead of a
    token — ``materialize`` records the user/member guids each
    materialization bound, the credential assertions MX's shape needs."""

    def __init__(self) -> None:
        self.accounts: list[providers.ProviderAccount] = []
        self.users_created: list[str] = []
        """ledger ids handed to create_user — laziness is its length."""
        self.sessions_created: list[dict] = []
        self.completed: list[dict] = []
        self.removed: list[dict] = []
        self.materialized: list[dict] = []
        self.user_guid: str | None = None
        self.member_guid: str | None = None

    def materialize(
        self, provider, *, user_guid: str | None = None, member_guid: str | None = None
    ) -> "FakeMXProvider":
        self.materialized.append(
            {"provider": provider, "user_guid": user_guid, "member_guid": member_guid}
        )
        self.user_guid = user_guid
        self.member_guid = member_guid
        return self

    async def create_user(self, *, ledger_id: str) -> str:
        self.users_created.append(ledger_id)
        return f"USR-{len(self.users_created)}"

    async def create_connect_session(self, *, client_user_id: str) -> str:
        self.sessions_created.append({"user_guid": self.user_guid, "member_guid": self.member_guid})
        return "https://int-widgets.moneydesktop.com/md/connect/fake"

    async def complete_connect(self, token: str) -> providers.ConnectResult:
        self.completed.append({"token": token, "user_guid": self.user_guid})
        self.member_guid = token
        return providers.ConnectResult(
            provider_item_id=token,
            provider_institution_id="mxbank",
            institution_name="MX Bank",
            secret=None,
        )

    async def get_accounts(self) -> list[providers.ProviderAccount]:
        return self.accounts

    async def remove_item(self) -> None:
        self.removed.append({"user_guid": self.user_guid, "member_guid": self.member_guid})


@pytest.fixture
def mx_provider(monkeypatch):
    """An MX-configured instance over the faked registry. Deliberately NO
    encryption key: MX must never need it (PRD #86 story 17)."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "mx_client_id", "test-mx-client-id")
    monkeypatch.setattr(settings, "mx_api_key", "test-mx-api-key")
    fake = FakeMXProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


def _script_mx_accounts(fake: FakeMXProvider) -> None:
    fake.accounts = [
        providers.ProviderAccount(
            provider_account_id="ACT-checking",
            name="MX Checking",
            kind="depository",
            currency="USD",
            mask="4821",
            balance_minor=150_025,
        ),
        providers.ProviderAccount(
            provider_account_id="ACT-card",
            name="MX Credit Card",
            kind="credit",
            currency="USD",
            balance_minor=-10_050,  # the client already signed it per segment
        ),
    ]


async def _connect_mx(client, fake: FakeMXProvider) -> dict:
    _script_mx_accounts(fake)
    session = await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    assert session.status_code == 201, session.text
    response = await client.post(
        CONNECTIONS, json={"provider": "mx", "token": "MBR-9"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_mx_connect_session_creates_enrollment_lazily(client, db, mx_provider) -> None:
    """The first MX connect session mints the provider-side user and the
    Enrollment row (CONTEXT.md: Enrollment); the second reuses both —
    exactly one container per (provider, ledger), one create_user ever."""
    await _signup(client)
    assert await Enrollment.where(lambda e: e.provider == ConnectionProvider.MX).all() == []

    first = await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    assert first.status_code == 201, first.text
    assert first.json() == {
        "provider": "mx",
        "token": "https://int-widgets.moneydesktop.com/md/connect/fake",
    }
    rows = await Enrollment.where(lambda e: e.provider == ConnectionProvider.MX).all()
    assert len(rows) == 1
    assert rows[0].provider_user_id == "USR-1"
    assert rows[0].ledger_id is not None
    ledger_id = str(rows[0].ledger_id)
    assert mx_provider.users_created == [ledger_id]  # the dashboard breadcrumb is the ledger
    assert mx_provider.sessions_created[-1] == {"user_guid": "USR-1", "member_guid": None}

    second = await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    assert second.status_code == 201
    assert mx_provider.users_created == [ledger_id]  # no second mint
    assert len(await Enrollment.where(lambda e: e.provider == ConnectionProvider.MX).all()) == 1
    assert mx_provider.sessions_created[-1] == {"user_guid": "USR-1", "member_guid": None}


async def test_enrollment_is_unique_per_provider_and_ledger(client, db, mx_provider) -> None:
    """The composite unique is the one-container law at the DB: a second
    row for the same (provider, ledger) refuses."""
    await _signup(client)
    await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    enrollment = await Enrollment.where(lambda e: e.provider == ConnectionProvider.MX).first()
    assert enrollment is not None and enrollment.ledger_id is not None
    ledger_id = enrollment.ledger_id
    ledger = await Ledger.where(lambda led: led.id == ledger_id).first()
    assert ledger is not None
    with pytest.raises(UniqueViolationError):
        await Enrollment.create(
            ledger=ledger, provider=ConnectionProvider.MX, provider_user_id="USR-dupe"
        )


async def test_mx_connect_creates_connection_with_null_secret_and_balances(
    client, db, mx_provider, run_jobs
) -> None:
    """The acceptance shape: verified member becomes the Connection
    (provider_item_id = member guid, encrypted_secret honestly NULL —
    ADR 0009), one Account per MX account, and balances land at connect —
    aggregation finished before the widget answered, so there is nothing
    to wait for. The auto-enqueued initial sync is CP3's quiet skip:
    no crash, no recorded error, health untouched."""
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    assert body["provider"] == "mx"
    assert body["status"] == "active"
    assert body["institution_name"] == "MX Bank"
    assert body["provider_institution_id"] == "mxbank"
    assert mx_provider.completed == [{"token": "MBR-9", "user_guid": "USR-1"}]

    row = await Connection.where(lambda c: c.provider_item_id == "MBR-9").first()
    assert row is not None
    assert row.provider == ConnectionProvider.MX
    assert row.encrypted_secret is None

    labels = {a["label"]: a for a in body["accounts"]}
    assert set(labels) == {"MX Checking", "MX Credit Card"}
    assert labels["MX Checking"]["kind"] == "depository"
    assert labels["MX Checking"]["mask"] == "4821"
    assert labels["MX Credit Card"]["kind"] == "credit"

    checking = labels["MX Checking"]["id"]
    entries = (await client.get(f"/api/v1/accounts/{checking}/balance-entries")).json()["items"]
    assert [e["amount_minor"] for e in entries] == [150_025]
    assert entries[0]["source"] == "provider"
    card = labels["MX Credit Card"]["id"]
    entries = (await client.get(f"/api/v1/accounts/{card}/balance-entries")).json()["items"]
    assert [e["amount_minor"] for e in entries] == [-10_050]

    # The initial-sync enqueue stands (provider-neutral doorbell shape);
    # running it is CP3's quiet skip — never an error on the connection.
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None
    assert health["last_synced_at"] is None  # nothing synced yet: CP3's story


async def test_mx_complete_without_enrollment_is_400(client, db, mx_provider) -> None:
    """A completion token with no enrollment cannot be ours — no session
    was ever minted here. A client bug, never a provider call."""
    await _signup(client)
    response = await client.post(
        CONNECTIONS, json={"provider": "mx", "token": "MBR-9"}, headers=await _csrf(client)
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "No MX enrollment for this ledger"
    assert mx_provider.completed == []


async def test_mx_complete_rejects_a_foreign_member_guid(client, db, mx_provider) -> None:
    """The never-trust-a-client-guid verdict surfaces as the client's
    fault: MEMBER_NOT_FOUND → 400, naming MX and the code only."""

    async def refuse(token: str):
        raise providers.ProviderError("MEMBER_NOT_FOUND", "member is not under this enrollment")

    mx_provider.complete_connect = refuse
    await _signup(client)
    await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    response = await client.post(
        CONNECTIONS, json={"provider": "mx", "token": "MBR-stolen"}, headers=await _csrf(client)
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "MX request failed: MEMBER_NOT_FOUND"


async def test_mx_repair_session_binds_the_member(client, db, mx_provider) -> None:
    """MX repair needs no stored secret (there is none, honestly): the
    binding is the enrollment user plus the connection's member guid —
    the credential gate is per-provider, so the Plaid tokenless-row 400
    never fires here."""
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "mx", "connection_id": body["id"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    assert mx_provider.sessions_created[-1] == {"user_guid": "USR-1", "member_guid": "MBR-9"}


async def test_mx_disconnect_deletes_the_member_and_severs(client, db, mx_provider) -> None:
    """Disconnect keeps its contract (PRD #86 story 10): the MX-side
    member is deleted under our enrollment, the connection severs, the
    accounts live on as manual — and the enrollment persists, the
    ledger's container for the next connect."""
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    response = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert response.status_code == 204, response.text
    assert mx_provider.removed == [{"user_guid": "USR-1", "member_guid": "MBR-9"}]
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).status_code == 404
    accounts = (await client.get("/api/v1/accounts")).json()["items"]
    assert {a["label"] for a in accounts} == {"MX Checking", "MX Credit Card"}
    assert all(a["manual"] is True for a in accounts)
    assert len(await Enrollment.where(lambda e: e.provider == ConnectionProvider.MX).all()) == 1


async def test_mx_disconnect_member_already_gone_still_severs(client, db, mx_provider) -> None:
    """MEMBER_NOT_FOUND is Plaid's ITEM_NOT_FOUND analog: the provider
    already not knowing the member is success, the sever proceeds."""

    async def already_gone() -> None:
        raise providers.ProviderError("MEMBER_NOT_FOUND", "member already gone")

    mx_provider.remove_item = already_gone
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    response = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert response.status_code == 204
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).status_code == 404


async def test_catalog_reports_mx_configured(client, db, mx_provider) -> None:
    """The MX entry flips on its own settings (PRD #86 stories 11/16):
    configured with its capability atoms, while keyless Plaid stays
    honestly off."""
    await _signup(client)
    entries = {e["provider"]: e for e in (await client.get(f"{CONNECTIONS}/providers")).json()}
    assert entries["mx"]["configured"] is True
    assert entries["mx"]["capabilities"] == ["transactions", "balances"]
    assert entries["plaid"]["configured"] is False
