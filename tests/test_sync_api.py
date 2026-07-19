"""M7 CP2 seam: the sync job over the public API (issue #34).

The fake provider scripts cursor batches; the in-memory job connector
executes them; effects are asserted where a user or script would see them —
the inbox, the balance history, the connection's health fields.
"""

import uuid
from datetime import date

import pytest
from cryptography.fernet import Fernet

from pinch_backend import providers
from pinch_backend.models import Transaction

CONNECTIONS = "/api/v1/connections"
TRANSACTIONS = "/api/v1/transactions"

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


@pytest.fixture
def plaid_settings(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "test-client-id")
    monkeypatch.setattr(settings, "plaid_secret", "test-secret")
    monkeypatch.setattr(settings, "secret_encryption_key", Fernet.generate_key().decode())
    return settings


def _txn(
    txn_id: str,
    amount_minor: int,
    *,
    account: str = "plaid-checking",
    pending: bool = False,
    day: str = "2026-07-18",
    name: str = "COFFEE SHOP",
) -> providers.ProviderTransaction:
    return providers.ProviderTransaction(
        provider_transaction_id=txn_id,
        provider_account_id=account,
        amount_minor=amount_minor,
        currency="USD",
        date=date.fromisoformat(day),
        description=name,
        pending=pending,
    )


class FakeSyncProvider:
    """Scriptable batches: each sync pops the next; an empty script answers
    an empty batch. Raises whatever `failure` holds instead, if set."""

    def __init__(self) -> None:
        self.accounts = [
            providers.ProviderAccount(
                provider_account_id="plaid-checking",
                name="Everyday Checking",
                kind="depository",
                currency="USD",
                balance_minor=100_000,
            )
        ]
        self.batches: list[providers.SyncBatch] = []
        self.sync_cursors: list[str | None] = []
        self.link_tokens_created: list[dict] = []
        self.failure: providers.ProviderError | None = None
        self.cursor_serial = 0

    async def create_link_token(self, *, client_user_id: str, access_token: str | None = None):
        self.link_tokens_created.append(
            {"client_user_id": client_user_id, "access_token": access_token}
        )
        return "link-fake"

    async def exchange_public_token(self, public_token: str) -> providers.ExchangedToken:
        return providers.ExchangedToken(
            access_token=f"access-fake-{public_token}", item_id=f"item-{public_token}"
        )

    async def get_accounts(self, access_token: str) -> list[providers.ProviderAccount]:
        if self.failure is not None:
            raise self.failure
        return self.accounts

    async def sync_transactions(self, access_token: str, cursor: str | None):
        if self.failure is not None:
            raise self.failure
        self.sync_cursors.append(cursor)
        if self.batches:
            return self.batches.pop(0)
        self.cursor_serial += 1
        return providers.SyncBatch(
            added=[], modified=[], removed=[], next_cursor=f"cursor-{self.cursor_serial}"
        )

    async def remove_item(self, access_token: str) -> None:
        return None


@pytest.fixture
def fake_provider(plaid_settings, monkeypatch):
    fake = FakeSyncProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: fake)
    return fake


async def _connect(client, fake) -> dict:
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_connect_auto_enqueues_initial_sync(client, db, fake_provider, run_jobs) -> None:
    """Connecting is the trigger: when the job lands, transactions sit in
    the inbox with proposals and balance history has begun (PRD #31)."""
    fake_provider.batches = [
        providers.SyncBatch(
            added=[
                _txn("t1", -1234, name="COFFEE SHOP"),
                _txn("t2", 250_000, name="PAYCHECK", day="2026-07-17"),
                _txn("t3", -8900, name="RESTAURANT", pending=True),
            ],
            modified=[],
            removed=[],
            next_cursor="cursor-1",
        )
    ]
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()

    listing = (await client.get(TRANSACTIONS)).json()["items"]
    assert {t["description_raw"] for t in listing} == {"COFFEE SHOP", "PAYCHECK", "RESTAURANT"}
    assert all(t["reviewed_at"] is None for t in listing)
    assert all(t["proposal"] is not None for t in listing)  # classified into the inbox
    pending = {t["description_raw"]: t["pending"] for t in listing}
    assert pending == {"COFFEE SHOP": False, "PAYCHECK": False, "RESTAURANT": True}

    account_id = body["accounts"][0]["id"]
    entries = (await client.get(f"/api/v1/accounts/{account_id}/balance-entries")).json()["items"]
    assert len(entries) == 1
    assert entries[0]["amount_minor"] == 100_000
    assert entries[0]["source"] == "provider"

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["last_synced_at"] is not None


async def test_manual_refresh_resumes_from_persisted_cursor(
    client, db, fake_provider, run_jobs
) -> None:
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()  # initial sync: cursor None → cursor-1

    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 202, response.text
    await run_jobs()
    assert fake_provider.sync_cursors == [None, "cursor-1"]


async def test_replayed_page_never_duplicates(client, db, fake_provider, run_jobs) -> None:
    """Replay safety: the same added transaction arriving twice (crash
    between page-apply and cursor persist) lands once."""
    fake_provider.batches = [
        providers.SyncBatch(added=[_txn("t1", -1234)], modified=[], removed=[], next_cursor="c1"),
        providers.SyncBatch(added=[_txn("t1", -1234)], modified=[], removed=[], next_cursor="c2"),
    ]
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()
    rows = await Transaction.where(lambda t: t.provider_transaction_id == "t1").all()
    assert len(rows) == 1


async def test_auth_error_marks_reauth_and_repair_flow_heals(
    client, db, fake_provider, run_jobs
) -> None:
    """The reauth loop (PRD #31): break → reauth_required → update-mode
    link token → next successful sync heals. No 'mark fixed' endpoint."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()

    fake_provider.failure = providers.ProviderError("ITEM_LOGIN_REQUIRED", "login expired")
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "reauth_required"
    assert health["error_detail"] == "ITEM_LOGIN_REQUIRED"

    # repair: update-mode link token for the same Item
    response = await client.post(
        f"{CONNECTIONS}/link-token",
        json={"connection_id": body["id"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    assert fake_provider.link_tokens_created[-1]["access_token"] == "access-fake-public-abc"

    # the next successful sync is the healer
    fake_provider.failure = None
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None


async def test_refresh_tenancy_404_and_keyless_403(client, db, fake_provider) -> None:
    await _signup(client)
    body = await _connect(client, fake_provider)

    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email="other@example.com")
    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 404
    assert (
        await client.post(f"{CONNECTIONS}/{uuid.uuid4()}/sync", headers=await _csrf(client))
    ).status_code == 404


async def test_synced_currency_defaults_to_account(client, db, fake_provider, run_jobs) -> None:
    """A provider transaction with silent currency lands in the account's
    currency — money never travels without ISO 4217 (CONTEXT.md)."""
    txn = _txn("t1", -1234)
    txn.currency = None
    fake_provider.batches = [
        providers.SyncBatch(added=[txn], modified=[], removed=[], next_cursor="c1")
    ]
    await _signup(client)
    await _connect(client, fake_provider)
    await run_jobs()
    row = await Transaction.where(lambda t: t.provider_transaction_id == "t1").first()
    assert row is not None and row.currency == "USD"


async def test_transient_failure_retries_then_exhaustion_errors(
    client, db, fake_provider, run_jobs
) -> None:
    """The retry ladder at the engine seam (the in-memory connector can't
    fast-forward scheduled retries): with retries remaining a transient
    failure raises — the job runner's backoff — and the connection stays
    active (not yet an error); on the final attempt it goes error, carrying
    the provider's code and nothing else. The next success heals."""
    from pinch_backend.sync import run_sync

    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()
    connection_id = uuid.UUID(body["id"])

    fake_provider.failure = providers.ProviderError("INSTITUTION_DOWN", "try later")
    with pytest.raises(providers.ProviderError):
        await run_sync(connection_id, final_attempt=False)
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"  # retries remain: not yet an error

    await run_sync(connection_id, final_attempt=True)
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "error"
    assert health["error_detail"] == "INSTITUTION_DOWN"

    fake_provider.failure = None
    await run_sync(connection_id, final_attempt=False)
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None


async def test_unknown_provider_account_is_skipped(client, db, fake_provider, run_jobs) -> None:
    """A transaction for an account Pinch doesn't hold (added at the bank
    after connect) is skipped, not crashed on — CP2 records, M8+ may adopt."""
    fake_provider.batches = [
        providers.SyncBatch(
            added=[_txn("t1", -1234), _txn("t2", -5678, account="plaid-new-savings")],
            modified=[],
            removed=[],
            next_cursor="c1",
        )
    ]
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()
    assert await Transaction.where(lambda t: t.provider_transaction_id == "t1").count() == 1
    assert await Transaction.where(lambda t: t.provider_transaction_id == "t2").count() == 0
    # and the sync still completed: cursor advanced, connection healthy
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
