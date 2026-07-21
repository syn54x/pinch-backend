"""M7 CP3 seam: replacement & removal over the public API (issue #35).

M6's ground-shift contracts as running code: posted-replaces-pending and
provider-modified are in-place rewrites of the same row; what was built on
the amount dies with it; sync-removed retracts through the import-undo
dissolution seam. All driven by scripted fake-provider batches.
"""

import uuid
from datetime import date

import pytest
from cryptography.fernet import Fernet

from pinch_backend import providers
from pinch_backend.models import Transaction

CONNECTIONS = "/api/v1/connections"
TRANSACTIONS = "/api/v1/transactions"
TRANSFERS = "/api/v1/transfers"
CORRECTION_LOG = "/api/v1/correction-log"

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
    replaces: str | None = None,
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
        pending_provider_transaction_id=replaces,
    )


def _batch(added=(), modified=(), removed=(), cursor="c-next") -> providers.SyncBatch:
    return providers.SyncBatch(
        added=list(added), modified=list(modified), removed=list(removed), next_cursor=cursor
    )


class FakeSyncProvider:
    def __init__(self) -> None:
        self.accounts = [
            providers.ProviderAccount(
                provider_account_id="plaid-checking",
                name="Everyday Checking",
                kind="depository",
                currency="USD",
                balance_minor=100_000,
            ),
            providers.ProviderAccount(
                provider_account_id="plaid-savings",
                name="Savings",
                kind="depository",
                currency="USD",
                balance_minor=500_000,
            ),
        ]
        self.batches: list[providers.SyncBatch] = []
        self.cursor_serial = 0

    async def create_link_token(self, *, client_user_id: str, access_token: str | None = None):
        return "link-fake"

    async def get_institution_name(self, access_token: str) -> str | None:
        return "First Platypus Bank"

    async def exchange_public_token(self, public_token: str) -> providers.ExchangedToken:
        return providers.ExchangedToken(
            access_token=f"access-fake-{public_token}", item_id=f"item-{public_token}"
        )

    async def get_accounts(self, access_token: str) -> list[providers.ProviderAccount]:
        return self.accounts

    async def sync_transactions(self, access_token: str, cursor: str | None):
        if self.batches:
            return self.batches.pop(0)
        self.cursor_serial += 1
        return _batch(cursor=f"cursor-auto-{self.cursor_serial}")

    async def remove_item(self, access_token: str) -> None:
        return None


@pytest.fixture
def fake_provider(plaid_settings, monkeypatch):
    fake = FakeSyncProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: fake)
    return fake


async def _connect_and_sync(client, fake, first_batch) -> dict:
    """Connect (auto-enqueues the initial sync) with the first batch
    scripted, then run it."""
    fake.batches = [first_batch]
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _refresh(client, connection_id: str) -> None:
    response = await client.post(f"{CONNECTIONS}/{connection_id}/sync", headers=await _csrf(client))
    assert response.status_code == 202, response.text


async def _listing(client) -> list[dict]:
    return (await client.get(TRANSACTIONS)).json()["items"]


async def _one_txn(client, description: str) -> dict:
    matches = [t for t in await _listing(client) if t["description_raw"] == description]
    assert len(matches) == 1, f"{description}: {len(matches)} matches"
    return matches[0]


async def _make_category(client, name: str) -> str:
    response = await client.post(
        "/api/v1/categories", json={"name": name}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _review(client, txn_id: str, body: dict | None = None) -> None:
    response = await client.post(
        f"/api/v1/transactions/{txn_id}/review", json=body, headers=await _csrf(client)
    )
    assert response.status_code == 200, response.text


# --- pending → posted -----------------------------------------------------


async def test_posting_with_equal_amount_inherits_everything(
    client, db, fake_provider, run_jobs
) -> None:
    """The replacement is an in-place rewrite of the same row: user data,
    reviewed state, and the Pinch id all stand when the amount is equal."""
    await _signup(client)
    body = await _connect_and_sync(
        client, fake_provider, _batch(added=[_txn("t-p1", -1234, pending=True)])
    )
    await run_jobs()
    pending_txn = await _one_txn(client, "COFFEE SHOP")
    assert pending_txn["pending"] is True
    groceries = await _make_category(client, "Groceries")
    await _review(client, pending_txn["id"], {"category_id": groceries})
    patch = await client.patch(
        f"/api/v1/transactions/{pending_txn['id']}",
        json={"tags": ["morning-ritual"], "display_name": "Morning Coffee", "notes": "the usual"},
        headers=await _csrf(client),
    )
    assert patch.status_code == 200, patch.text

    fake_provider.batches = [
        _batch(
            added=[_txn("t-post1", -1234, replaces="t-p1", name="COFFEE SHOP POSTED")],
            removed=["t-p1"],
        )
    ]
    await _refresh(client, body["id"])
    await run_jobs()

    listing = await _listing(client)
    assert len(listing) == 1  # replaced, never duplicated — the removal was the replacement
    posted = listing[0]
    assert posted["id"] == pending_txn["id"]  # same Pinch row
    assert posted["pending"] is False
    assert posted["description_raw"] == "COFFEE SHOP POSTED"
    assert posted["reviewed_at"] is not None  # review work never destroyed
    assert posted["category"]["name"] == "Groceries"
    # user data survives wholesale: tags, display name, notes (AC1)
    assert [t["name"] for t in posted["tags"]] == ["morning-ritual"]
    assert posted["display_name"] == "Morning Coffee"
    assert posted["notes"] == "the usual"
    row = await Transaction.where(lambda t: t.provider_transaction_id == "t-post1").first()
    assert row is not None and row.id == uuid.UUID(pending_txn["id"])


async def test_posting_with_changed_amount_reopens_and_dissolves_splits(
    client, db, fake_provider, run_jobs
) -> None:
    """What was built on the amount dies: split lines deleted, review
    reopened, decisions voided, a fresh proposal in the inbox."""
    await _signup(client)
    body = await _connect_and_sync(
        client, fake_provider, _batch(added=[_txn("t-p1", -10_000, pending=True)])
    )
    await run_jobs()
    txn = await _one_txn(client, "COFFEE SHOP")
    groceries = await _make_category(client, "Groceries")
    household = await _make_category(client, "Household")
    await _review(
        client,
        txn["id"],
        {
            "splits": [
                {"amount_minor": -6_000, "category_id": groceries},
                {"amount_minor": -4_000, "category_id": household},
            ]
        },
    )

    fake_provider.batches = [
        _batch(added=[_txn("t-post1", -10_500, replaces="t-p1")], removed=["t-p1"])
    ]
    await _refresh(client, body["id"])
    await run_jobs()

    posted = (await _listing(client))[0]
    assert posted["id"] == txn["id"]
    assert posted["amount_minor"] == -10_500
    assert posted["splits"] is None  # lines died with the amount
    assert posted["category"] is None  # parent was vacated; nothing restores it
    assert posted["reviewed_at"] is None  # back in the inbox
    assert posted["proposal"] is not None  # freshly classified
    voids = (await client.get(f"{CORRECTION_LOG}?kind=void")).json()["items"]
    assert any(v["void_reason"] and "amount" in v["void_reason"] for v in voids)
    # append-only in full (AC6): the original decision entries still stand,
    # un-edited — a void is a later entry pointing back, never a mutation
    decisions = (await client.get(f"{CORRECTION_LOG}?kind=decision")).json()["items"]
    assert len(decisions) >= 1
    voided_targets = {v["voids"] for v in voids}
    assert any(d["id"] in voided_targets for d in decisions)


async def test_modified_cosmetic_change_never_reopens(client, db, fake_provider, run_jobs) -> None:
    """Description drift is cosmetic: source data updates, review stands."""
    await _signup(client)
    body = await _connect_and_sync(client, fake_provider, _batch(added=[_txn("t1", -1234)]))
    await run_jobs()
    txn = await _one_txn(client, "COFFEE SHOP")
    await _review(client, txn["id"])

    fake_provider.batches = [_batch(modified=[_txn("t1", -1234, name="Coffee Shop #42 Cleaned")])]
    await _refresh(client, body["id"])
    await run_jobs()

    updated = (await _listing(client))[0]
    assert updated["description_raw"] == "Coffee Shop #42 Cleaned"
    assert updated["reviewed_at"] is not None


async def test_modified_amount_change_dissolves_transfer_both_sides(
    client, db, fake_provider, run_jobs
) -> None:
    """The linked pair dies with the amount: link dissolved, both sides
    reopened, transfer decisions voided (M6 ground-shift, verbatim)."""
    await _signup(client)
    body = await _connect_and_sync(
        client,
        fake_provider,
        _batch(
            added=[
                _txn("t-out", -50_000, name="TRANSFER TO SAVINGS"),
                _txn("t-in", 50_000, account="plaid-savings", name="TRANSFER FROM CHECKING"),
            ]
        ),
    )
    await run_jobs()
    out_txn = await _one_txn(client, "TRANSFER TO SAVINGS")
    in_txn = await _one_txn(client, "TRANSFER FROM CHECKING")
    link = await client.post(
        TRANSFERS,
        json={"transaction_ids": [out_txn["id"], in_txn["id"]]},
        headers=await _csrf(client),
    )
    assert link.status_code == 201, link.text
    await _review(client, out_txn["id"])
    await _review(client, in_txn["id"])

    fake_provider.batches = [_batch(modified=[_txn("t-out", -49_000, name="TRANSFER TO SAVINGS")])]
    await _refresh(client, body["id"])
    await run_jobs()

    assert (await client.get(TRANSFERS)).json()["items"] == []
    out_after = await _one_txn(client, "TRANSFER TO SAVINGS")
    in_after = await _one_txn(client, "TRANSFER FROM CHECKING")
    assert out_after["amount_minor"] == -49_000
    assert out_after["transfer"] is None and in_after["transfer"] is None
    assert out_after["reviewed_at"] is None and in_after["reviewed_at"] is None


async def test_equal_amount_rewrite_preserves_transfer_link_and_date_drift(
    client, db, fake_provider, run_jobs
) -> None:
    """AC2 in full: an amount-preserving rewrite — even one shifting the
    date — leaves transfer links and reviewed state standing. Settlement
    lag moving a date is cosmetic; only the amount is material."""
    await _signup(client)
    body = await _connect_and_sync(
        client,
        fake_provider,
        _batch(
            added=[
                _txn("t-out", -50_000, name="TRANSFER TO SAVINGS"),
                _txn("t-in", 50_000, account="plaid-savings", name="TRANSFER FROM CHECKING"),
            ]
        ),
    )
    await run_jobs()
    out_txn = await _one_txn(client, "TRANSFER TO SAVINGS")
    in_txn = await _one_txn(client, "TRANSFER FROM CHECKING")
    assert (
        await client.post(
            TRANSFERS,
            json={"transaction_ids": [out_txn["id"], in_txn["id"]]},
            headers=await _csrf(client),
        )
    ).status_code == 201
    await _review(client, out_txn["id"])
    await _review(client, in_txn["id"])

    fake_provider.batches = [
        _batch(modified=[_txn("t-out", -50_000, name="TRANSFER TO SAVINGS", day="2026-07-20")])
    ]
    await _refresh(client, body["id"])
    await run_jobs()

    out_after = await _one_txn(client, "TRANSFER TO SAVINGS")
    assert out_after["date"] == "2026-07-20"  # source data updated
    assert out_after["transfer"] is not None  # the link survives the drift
    assert out_after["reviewed_at"] is not None  # review stands
    assert (await client.get(TRANSFERS)).json()["items"] != []


# --- removed --------------------------------------------------------------


async def test_removed_retracts_like_import_undo(client, db, fake_provider, run_jobs) -> None:
    """Sync-removed and import-undo share one dissolution seam: row gone,
    transfer dissolved, surviving counterpart reopened, decision voided."""
    await _signup(client)
    body = await _connect_and_sync(
        client,
        fake_provider,
        _batch(
            added=[
                _txn("t-out", -50_000, name="TRANSFER TO SAVINGS"),
                _txn("t-in", 50_000, account="plaid-savings", name="TRANSFER FROM CHECKING"),
            ]
        ),
    )
    await run_jobs()
    out_txn = await _one_txn(client, "TRANSFER TO SAVINGS")
    in_txn = await _one_txn(client, "TRANSFER FROM CHECKING")
    assert (
        await client.post(
            TRANSFERS,
            json={"transaction_ids": [out_txn["id"], in_txn["id"]]},
            headers=await _csrf(client),
        )
    ).status_code == 201
    await _review(client, out_txn["id"])
    await _review(client, in_txn["id"])

    fake_provider.batches = [_batch(removed=["t-out"])]
    await _refresh(client, body["id"])
    await run_jobs()

    listing = await _listing(client)
    assert {t["description_raw"] for t in listing} == {"TRANSFER FROM CHECKING"}
    survivor = listing[0]
    assert survivor["transfer"] is None
    assert survivor["reviewed_at"] is None  # reopened, back in the inbox
    assert survivor["proposal"] is not None  # freshly classified
    voids = (await client.get(f"{CORRECTION_LOG}?kind=void")).json()["items"]
    assert any(v["void_reason"] and "sync" in v["void_reason"] for v in voids)


async def test_replayed_replacement_batch_is_idempotent(
    client, db, fake_provider, run_jobs
) -> None:
    """A replayed page (crash between apply and cursor persist) re-applies
    the replacement harmlessly: one row, posted, same id."""
    await _signup(client)
    body = await _connect_and_sync(
        client, fake_provider, _batch(added=[_txn("t-p1", -1234, pending=True)])
    )
    await run_jobs()
    original = await _one_txn(client, "COFFEE SHOP")

    replacement = _batch(
        added=[_txn("t-post1", -1234, replaces="t-p1")], removed=["t-p1"], cursor="c-replay"
    )
    fake_provider.batches = [replacement]
    await _refresh(client, body["id"])
    await run_jobs()
    fake_provider.batches = [replacement.model_copy(deep=True)]
    await _refresh(client, body["id"])
    await run_jobs()

    listing = await _listing(client)
    assert len(listing) == 1
    assert listing[0]["id"] == original["id"]
    assert listing[0]["pending"] is False
