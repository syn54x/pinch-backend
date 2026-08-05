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
    an empty batch. Raises whatever `failure` holds instead, if set.
    ``materialize`` stands in for ``get_provider`` (M13 CP1), recording
    the bound secret the method signatures no longer carry."""

    def __init__(self) -> None:
        self.accounts = [
            providers.ProviderAccount(
                provider_account_id="plaid-checking",
                mask="4821",
                name="Everyday Checking",
                kind="depository",
                currency="USD",
                balance_minor=100_000,
            )
        ]
        self.batches: list[providers.SyncBatch] = []
        self.sync_cursors: list[str | None] = []
        self.sessions_created: list[dict] = []
        self.failure: providers.ProviderError | None = None
        self.transactions_failure: providers.ProviderError | None = None
        """Raises from sync_transactions only, accounts stay healthy —
        the fresh-Item PRODUCT_NOT_READY shape."""
        self.cursor_serial = 0
        self.secret: str | None = None
        """The most recent materialization's bound credential."""

        self.institution_lookups = 0

    def materialize(self, provider, *, secret: str | None = None) -> "FakeSyncProvider":
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

    async def create_connect_session(self, *, client_user_id: str) -> str:
        self.sessions_created.append({"client_user_id": client_user_id, "secret": self.secret})
        return "link-fake"

    async def complete_connect(self, token: str) -> providers.ConnectResult:
        self.secret = f"access-fake-{token}"
        return providers.ConnectResult(
            provider_item_id=f"item-{token}",
            provider_institution_id="ins_platypus",
            institution_name="First Platypus Bank",
            secret=self.secret,
        )

    async def get_accounts(self) -> list[providers.ProviderAccount]:
        if self.failure is not None:
            raise self.failure
        return self.accounts

    async def sync_transactions(self, cursor: str | None):
        if self.failure is not None:
            raise self.failure
        if self.transactions_failure is not None:
            raise self.transactions_failure
        self.sync_cursors.append(cursor)
        if self.batches:
            return self.batches.pop(0)
        self.cursor_serial += 1
        return providers.SyncBatch(
            added=[], modified=[], removed=[], next_cursor=f"cursor-{self.cursor_serial}"
        )

    async def remove_item(self) -> None:
        return None

    async def get_institution(self) -> providers.ProviderInstitution:
        self.institution_lookups += 1
        return providers.ProviderInstitution(
            provider_institution_id="ins_platypus", name="First Platypus Bank"
        )


@pytest.fixture
def fake_provider(plaid_settings, monkeypatch):
    fake = FakeSyncProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


async def _connect(client, fake) -> dict:
    response = await client.post(
        CONNECTIONS, json={"provider": "plaid", "token": "public-abc"}, headers=await _csrf(client)
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
    # Two provider entries: the connect-time observation (M13 CP2 — the
    # provider handed balances over with the accounts) and the initial
    # sync's; same value, both honest reads.
    assert len(entries) == 2
    assert all(e["amount_minor"] == 100_000 for e in entries)
    assert all(e["source"] == "provider" for e in entries)

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["last_synced_at"] is not None


async def test_manual_refresh_resumes_from_persisted_cursor(
    client, db, fake_provider, run_jobs
) -> None:
    """The refresh both resumes the cursor and lands genuinely new
    transactions in the inbox."""
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()  # initial sync: cursor None → cursor-1
    fake_provider.batches = [
        providers.SyncBatch(
            added=[_txn("t-new", -4200, name="NEW SINCE CONNECT")],
            modified=[],
            removed=[],
            next_cursor="cursor-2",
        )
    ]

    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 202, response.text
    await run_jobs()
    assert fake_provider.sync_cursors == [None, "cursor-1"]
    listing = (await client.get(TRANSACTIONS)).json()["items"]
    assert "NEW SINCE CONNECT" in {t["description_raw"] for t in listing}


async def test_overlapping_refreshes_serialize_without_duplicates(
    client, db, fake_provider, run_jobs
) -> None:
    """AC4: two refreshes enqueued back-to-back (the lock's queue-never-
    conflict promise) process serially — same rows land exactly once."""
    fake_provider.batches = [
        providers.SyncBatch(added=[_txn("t1", -1234)], modified=[], removed=[], next_cursor="c1"),
        providers.SyncBatch(added=[_txn("t1", -1234)], modified=[], removed=[], next_cursor="c2"),
    ]
    await _signup(client)
    body = await _connect(client, fake_provider)
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()  # initial + refresh, serialized by the connection lock
    assert len(fake_provider.sync_cursors) == 2
    rows = await Transaction.where(lambda t: t.provider_transaction_id == "t1").all()
    assert len(rows) == 1


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

    # repair: a session bound to the same provider-side login — the
    # connection's decrypted credential rides the materialization (M13 CP1)
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "plaid", "connection_id": body["id"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    assert fake_provider.sessions_created[-1]["secret"] == "access-fake-public-abc"

    # the next successful sync is the healer
    fake_provider.failure = None
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None


async def test_refresh_tenancy_404(client, db, fake_provider) -> None:
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


async def test_product_not_ready_waits_quietly_for_the_doorbell(
    client, db, fake_provider, run_jobs, job_connector
) -> None:
    """Readiness retires (M11 CP1, issue #79): a fresh Item whose pull
    isn't finished is not a failure — the initial sync call armed the
    doorbell, so the connection waits active with a null last_synced_at
    (the UI's first-sync-in-progress rendering), never parked in error,
    never retried for readiness."""
    fake_provider.transactions_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="initial transaction pull not finished"
    )
    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None
    assert health["last_synced_at"] is None
    # No retry ladder for readiness: every job ran to completion — nothing
    # parked as a scheduled retry, nothing failed.
    assert {j["status"] for j in job_connector.jobs.values()} == {"succeeded"}


async def test_a_pre_m11_parked_readiness_error_heals_into_the_quiet_wait(
    client, db, fake_provider, run_jobs
) -> None:
    """A connection the old ladder parked as error: PRODUCT_NOT_READY (the
    Stash Item's own history) must not stay a lie under the quiet wait —
    the next not-ready answer clears it to active. A genuinely parked
    error (any other code) stays until a successful sync heals it."""
    from pinch_backend.models import Connection, ConnectionStatus
    from pinch_backend.sync import record_broken, run_sync

    await _signup(client)
    body = await _connect(client, fake_provider)
    await run_jobs()
    connection = await Connection.where(lambda c, cid=body["id"]: c.id == cid).first()
    await record_broken(connection, ConnectionStatus.ERROR, "PRODUCT_NOT_READY")

    fake_provider.transactions_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="initial transaction pull not finished"
    )
    await run_sync(uuid.UUID(body["id"]), final_attempt=False)

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


async def test_sync_backfills_missing_institution_identity(
    client, db, fake_provider, run_jobs
) -> None:
    """Pre-enabler connections gain their bank name — and pre-M13 rows
    their provider institution id (#88: backfill opportunistic) — on the
    next sync, from one shared lookup."""
    import uuid

    from pinch_backend.models import Connection

    await _signup(client)
    body = await _connect(client, fake_provider)
    cid = uuid.UUID(body["id"])
    connection = (await Connection.where(lambda c, cid=cid: c.id == cid).all())[0]
    connection.institution_name = None  # simulate a pre-enabler row
    connection.provider_institution_id = None  # ...that also predates M13
    await connection.save()
    from pinch_backend.models import Account

    checking = (await Account.where(lambda a: a.provider_account_id == "plaid-checking").all())[0]
    checking.mask = None  # pre-enabler account, no digits yet
    await checking.save()

    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 202
    await run_jobs()

    listed = (await client.get(CONNECTIONS)).json()["items"][0]
    assert listed["institution_name"] == "First Platypus Bank"
    assert listed["provider_institution_id"] == "ins_platypus"
    masks = {a["label"]: a["mask"] for a in listed["accounts"]}
    assert masks["Everyday Checking"] == "4821"


async def test_sync_backfills_the_id_of_a_named_pre_m13_row(
    client, db, fake_provider, run_jobs
) -> None:
    """The M13 shape specifically: a row whose name was captured long ago
    but whose provider_institution_id predates the column still gains the
    id — and the name the user may have relied on stands untouched."""
    import uuid

    from pinch_backend.models import Connection

    await _signup(client)
    body = await _connect(client, fake_provider)
    cid = uuid.UUID(body["id"])
    connection = (await Connection.where(lambda c, cid=cid: c.id == cid).all())[0]
    connection.institution_name = "A Name The User Knows"
    connection.provider_institution_id = None  # pre-M13 row
    await connection.save()

    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()

    listed = (await client.get(CONNECTIONS)).json()["items"][0]
    assert listed["provider_institution_id"] == "ins_platypus"
    assert listed["institution_name"] == "A Name The User Knows"  # never overwritten
