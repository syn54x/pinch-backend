"""M13 CP3 seam: MX transactions through the sync engine (issue #90).

The scriptable MX fake sits at the registry seam exactly where the Plaid
fake lives; effects are asserted where a user or script would see them —
the inbox, the balance history, the connection's health fields. What CP3
must prove here: MX rows flow into the same inbox/pipeline as Plaid's,
the date-watermark cursor persists only after a batch applies, replayed
overlap windows never duplicate (dedupe by guid — replay-safe by
construction), removal rides the M7 retraction semantics, member
statuses map into the existing three, repair heals through the next
successful sync, and no investments job ever chains for MX.
"""

import uuid
from datetime import date

import pytest

from pinch_backend import providers
from pinch_backend.models import Connection, ConnectionProvider, Enrollment, Transaction

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


def _txn(
    txn_id: str,
    amount_minor: int,
    *,
    account: str = "ACT-checking",
    day: str = "2026-08-01",
    name: str = "ROSS",
) -> providers.ProviderTransaction:
    """An MX-shaped provider transaction: already converted at the wire
    seam (signed, minor units, posted-only), riding ``modified`` — the
    re-derivation can't tell add from modify, so the upsert lane is the
    only lane."""
    return providers.ProviderTransaction(
        provider_transaction_id=txn_id,
        provider_account_id=account,
        amount_minor=amount_minor,
        currency="USD",
        date=date.fromisoformat(day),
        description=name,
        pending=False,
    )


def _batch(
    *,
    modified: list[providers.ProviderTransaction] | None = None,
    removed: list[str] | None = None,
    cursor: str = "2026-08-04",
) -> providers.SyncBatch:
    return providers.SyncBatch(
        added=[], modified=modified or [], removed=removed or [], next_cursor=cursor
    )  # fmt: skip


class FakeMXSyncProvider:
    """Scriptable MX at the registry seam: guid bindings instead of a
    token, batches in the MX shape (everything through ``modified``),
    and the engine-bound ``stored_window_ids`` recorded so tests can
    exercise the closure the engine hands the real client."""

    def __init__(self) -> None:
        self.accounts = [
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
                balance_minor=-10_050,
            ),
        ]
        self.batches: list[providers.SyncBatch] = []
        self.sync_cursors: list[str | None] = []
        self.sessions_created: list[dict] = []
        self.failure: providers.ProviderError | None = None
        self.users_created: list[str] = []
        self.materialized: list[dict] = []
        self.user_guid: str | None = None
        self.member_guid: str | None = None
        self.stored_window_ids = None

    def materialize(
        self,
        provider,
        *,
        user_guid: str | None = None,
        member_guid: str | None = None,
        stored_window_ids=None,
    ) -> "FakeMXSyncProvider":
        self.materialized.append(
            {
                "provider": provider,
                "user_guid": user_guid,
                "member_guid": member_guid,
                "stored_window_ids": stored_window_ids,
            }
        )
        self.user_guid = user_guid
        self.member_guid = member_guid
        if stored_window_ids is not None:
            self.stored_window_ids = stored_window_ids
        return self

    async def create_user(self, *, ledger_id: str) -> str:
        self.users_created.append(ledger_id)
        return "USR-1"

    async def create_connect_session(self, *, client_user_id: str) -> str:
        self.sessions_created.append({"user_guid": self.user_guid, "member_guid": self.member_guid})
        return "https://int-widgets.moneydesktop.com/md/connect/fake"

    async def complete_connect(self, token: str) -> providers.ConnectResult:
        self.member_guid = token
        return providers.ConnectResult(
            provider_item_id=token,
            provider_institution_id="mxbank",
            institution_name="MX Bank",
            secret=None,  # guid-shaped provider: honestly no secret (ADR 0009)
        )

    async def get_accounts(self) -> list[providers.ProviderAccount]:
        return self.accounts

    async def sync_transactions(self, cursor: str | None) -> providers.SyncBatch:
        if self.failure is not None:
            raise self.failure
        self.sync_cursors.append(cursor)
        if self.batches:
            return self.batches.pop(0)
        return _batch()

    async def get_institution(self) -> providers.ProviderInstitution:
        return providers.ProviderInstitution(provider_institution_id="mxbank", name="MX Bank")

    async def get_item_state(self) -> providers.ItemState:
        """The midnight-UTC reconcile cron can fire inside any test's
        worker window (the FakeInvestmentsProvider precedent). A healthy
        member with no aggregation stamp reads "quiet" — a no-op pass
        instead of an AttributeError'd job failing an unrelated test."""
        return providers.ItemState(webhook="", member_status="CONNECTED")

    async def remove_item(self) -> None:
        return None


@pytest.fixture
def mx_provider(monkeypatch):
    """An MX-configured instance over the faked registry — deliberately
    NO encryption key: the whole sync path must run secretless (PRD #86
    story 17; ``encrypted_secret`` is honestly NULL)."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "mx_client_id", "test-mx-client-id")
    monkeypatch.setattr(settings, "mx_api_key", "test-mx-api-key")
    fake = FakeMXSyncProvider()
    monkeypatch.setattr(providers, "get_provider", fake.materialize)
    return fake


async def _connect_mx(client, fake) -> dict:
    session = await client.post(
        f"{CONNECTIONS}/connect-session", json={"provider": "mx"}, headers=await _csrf(client)
    )
    assert session.status_code == 201, session.text
    response = await client.post(
        CONNECTIONS, json={"provider": "mx", "token": "MBR-9"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _refresh(client, connection_id: str) -> None:
    response = await client.post(f"{CONNECTIONS}/{connection_id}/sync", headers=await _csrf(client))
    assert response.status_code == 202, response.text


async def test_mx_initial_sync_lands_the_inbox_with_signed_rows(
    client, db, mx_provider, run_jobs, job_connector
) -> None:
    """The acceptance shape (issue #90): MX transactions land in the same
    inbox as Plaid's, with proposals, posted-only, correctly signed — and
    the connection's health reads exactly like a Plaid connection's. No
    investments job ever chains: MX's capability atoms exclude it."""
    mx_provider.batches = [
        _batch(
            modified=[
                _txn("TRN-1", -7233, name="ROSS"),
                _txn("TRN-2", 250_000, name="PAYCHECK", day="2026-07-31"),
                _txn("TRN-3", 2071, account="ACT-card", name="CREDIT CARD PAYMENT"),
            ]
        )
    ]
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()

    listing = (await client.get(TRANSACTIONS)).json()["items"]
    assert {t["description_raw"] for t in listing} == {"ROSS", "PAYCHECK", "CREDIT CARD PAYMENT"}
    assert all(t["reviewed_at"] is None for t in listing)
    assert all(t["proposal"] is not None for t in listing)  # classified into the inbox
    assert all(t["pending"] is False for t in listing)  # posted-only v1
    amounts = {t["description_raw"]: t["amount_minor"] for t in listing}
    assert amounts["CREDIT CARD PAYMENT"] == 2071  # CREDIT on the debt account: money in

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["last_synced_at"] is not None
    assert health["error_detail"] is None

    task_names = {j["task_name"] for j in job_connector.jobs.values()}
    assert "sync.sync_investments" not in task_names  # a stated provider limit, never a job


async def test_mx_balance_history_accumulates_from_first_sync(
    client, db, mx_provider, run_jobs
) -> None:
    """Balance entries accumulate per sync like Plaid's (issue #90 AC):
    the connect-time observation, then one per sync."""
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()  # initial sync
    await _refresh(client, body["id"])
    await run_jobs()

    checking = next(a for a in body["accounts"] if a["label"] == "MX Checking")["id"]
    entries = (await client.get(f"/api/v1/accounts/{checking}/balance-entries")).json()["items"]
    assert len(entries) == 3  # connect + initial sync + refresh
    assert all(e["amount_minor"] == 150_025 for e in entries)
    assert all(e["source"] == "provider" for e in entries)


async def test_mx_cursor_is_a_persisted_date_watermark(client, db, mx_provider, run_jobs) -> None:
    """The cursor column serializes the date watermark, persisted only
    after a batch fully applies: the next sync resumes from it, and a
    failed sync never advances it."""
    mx_provider.batches = [_batch(cursor="2026-08-03")]
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()

    cid = uuid.UUID(body["id"])
    connection = await Connection.where(lambda c: c.id == cid).first()
    assert connection is not None and connection.sync_cursor == "2026-08-03"
    assert connection.encrypted_secret is None  # secretless the whole way (story 17)

    mx_provider.failure = providers.ProviderError("FAILED", "aggregation failed")
    from pinch_backend.sync import run_sync

    await run_sync(cid, final_attempt=True)
    connection = await Connection.where(lambda c: c.id == cid).first()
    assert connection is not None and connection.sync_cursor == "2026-08-03"  # never advanced

    mx_provider.failure = None
    mx_provider.batches = [_batch(cursor="2026-08-05")]
    await _refresh(client, body["id"])
    await run_jobs()
    assert mx_provider.sync_cursors == [None, "2026-08-03"]  # resumed from the watermark
    connection = await Connection.where(lambda c: c.id == cid).first()
    assert connection is not None and connection.sync_cursor == "2026-08-05"


async def test_mx_replayed_overlap_window_never_duplicates(
    client, db, mx_provider, run_jobs
) -> None:
    """The inclusive day-granularity watermark makes overlap re-fetch the
    norm (CP0 spike): the same guid arriving again through ``modified``
    rewrites the same row — ingestion dedupes by provider guid, so replay
    is safe by construction, and an unchanged replay is cosmetic: review
    posture untouched."""
    mx_provider.batches = [
        _batch(modified=[_txn("TRN-1", -7233)]),
        _batch(modified=[_txn("TRN-1", -7233)]),  # the watermark day, re-fetched
    ]
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()
    await _refresh(client, body["id"])
    await run_jobs()

    rows = await Transaction.where(lambda t: t.provider_transaction_id == "TRN-1").all()
    assert len(rows) == 1
    assert rows[0].amount_minor == -7233


async def test_mx_removed_retracts_through_the_m7_seam(client, db, mx_provider, run_jobs) -> None:
    """A transaction deleted at MX inside the window arrives as
    ``removed`` (the window diff) and retracts through the existing
    sync-removed semantics (M7 CP3): the row is gone from the ledger,
    its neighbors stand."""
    mx_provider.batches = [
        _batch(modified=[_txn("TRN-keep", -1000), _txn("TRN-gone", -2000, name="GHOST")]),
        _batch(removed=["TRN-gone"]),
    ]
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()
    assert await Transaction.where(lambda t: t.provider_transaction_id == "TRN-gone").count() == 1

    await _refresh(client, body["id"])
    await run_jobs()
    assert await Transaction.where(lambda t: t.provider_transaction_id == "TRN-gone").count() == 0
    assert await Transaction.where(lambda t: t.provider_transaction_id == "TRN-keep").count() == 1
    listing = (await client.get(TRANSACTIONS)).json()["items"]
    assert {t["description_raw"] for t in listing} == {"ROSS"}


@pytest.mark.parametrize("status", ["CHALLENGED", "DENIED", "LOCKED"])
async def test_mx_reauth_statuses_map_and_repair_heals(
    client, db, mx_provider, run_jobs, status: str
) -> None:
    """The member-status mapping (issue #90): the scripted reauth family
    surfaces as reauth_required carrying the status — never a new
    connection status — and the repair loop is Plaid's exactly: a
    reconnect-mode session bound to the member, then the next successful
    sync is the healer. No 'mark fixed' endpoint."""
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()

    mx_provider.failure = providers.ProviderError(status, "MX member is not connected")
    await _refresh(client, body["id"])
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "reauth_required"
    assert health["error_detail"] == status

    # repair: a reconnect-mode widget session pinned to the member
    response = await client.post(
        f"{CONNECTIONS}/connect-session",
        json={"provider": "mx", "connection_id": body["id"]},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    assert mx_provider.sessions_created[-1]["member_guid"] == "MBR-9"

    # the widget fixed the login; the next successful sync heals
    mx_provider.failure = None
    await _refresh(client, body["id"])
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None


async def test_mx_failed_family_rides_the_ladder_to_error(
    client, db, mx_provider, run_jobs
) -> None:
    """FAILED is transient-shaped (MX self-aggregates nightly): retries
    remaining → raise into the job runner's backoff; exhaustion records
    ``error`` carrying the status; the next success heals."""
    from pinch_backend.sync import run_sync

    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await run_jobs()
    cid = uuid.UUID(body["id"])

    mx_provider.failure = providers.ProviderError("FAILED", "aggregation failed")
    with pytest.raises(providers.ProviderError):
        await run_sync(cid, final_attempt=False)
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"  # retries remain: not yet an error

    await run_sync(cid, final_attempt=True)
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "error"
    assert health["error_detail"] == "FAILED"

    mx_provider.failure = None
    await run_sync(cid, final_attempt=False)
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None


async def test_mx_engine_binds_enrollment_member_and_window_memory(
    client, db, mx_provider, run_jobs
) -> None:
    """The sync materialization carries MX's whole binding (ADR 0009):
    the enrollment's user guid, the connection's member guid, and the
    ``stored_window_ids`` closure — which answers exactly the provider
    ids Pinch holds in the window, so the client's removal diff sees
    truth without seeing the database."""
    mx_provider.batches = [
        _batch(
            modified=[
                _txn("TRN-old", -100, day="2026-01-15"),
                _txn("TRN-recent", -200, day="2026-08-01"),
            ]
        )
    ]
    await _signup(client)
    await _connect_mx(client, mx_provider)
    await run_jobs()

    sync_binding = mx_provider.materialized[-1]
    assert sync_binding["user_guid"] == "USR-1"
    assert sync_binding["member_guid"] == "MBR-9"
    closure = sync_binding["stored_window_ids"]
    assert closure is not None
    # The closure honors the window it is asked about: a start date after
    # the old row excludes it; a start date before both includes both.
    assert await closure(date.fromisoformat("2026-07-01")) == {"TRN-recent"}
    assert await closure(date.fromisoformat("2026-01-01")) == {"TRN-old", "TRN-recent"}


async def test_mx_connection_without_enrollment_is_loud(client, db, mx_provider) -> None:
    """Corrupted state (an MX connection with no enrollment) fails the
    job loudly instead of inventing a status: connects only ever ride an
    ensured enrollment."""
    from pinch_backend.sync import run_sync

    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await Enrollment.where(lambda e: e.provider == ConnectionProvider.MX).delete()
    with pytest.raises(RuntimeError, match="no enrollment"):
        await run_sync(uuid.UUID(body["id"]), final_attempt=False)


async def test_mx_refresh_tenancy_404(client, db, mx_provider) -> None:
    await _signup(client)
    body = await _connect_mx(client, mx_provider)
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email="other@example.com")
    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 404
