"""M7 CP4 seam: the transfer detector over the public API (issue #36).

The detector is a post-classification pass — sync, import commit, and
manual creation all funnel through the classify job, so every path gets
detection for free. Mirrored proposals carry the `detection` provenance
and a counterpart reference; one consent consumes both sides.
"""

from datetime import date

import pytest
from cryptography.fernet import Fernet

from pinch_backend import providers

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
    day: str = "2026-07-18",
    name: str = "MOVE MONEY",
    currency: str = "USD",
) -> providers.ProviderTransaction:
    return providers.ProviderTransaction(
        provider_transaction_id=txn_id,
        provider_account_id=account,
        amount_minor=amount_minor,
        currency=currency,
        date=date.fromisoformat(day),
        description=name,
        pending=False,
    )


def _batch(added=(), cursor="c-next") -> providers.SyncBatch:
    return providers.SyncBatch(added=list(added), modified=[], removed=[], next_cursor=cursor)


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
    fake.batches = [first_batch]
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _listing(client) -> list[dict]:
    return (await client.get(TRANSACTIONS)).json()["items"]


async def _one_txn(client, description: str) -> dict:
    matches = [t for t in await _listing(client) if t["description_raw"] == description]
    assert len(matches) == 1, f"{description}: {len(matches)} matches"
    return matches[0]


PAIR = [
    _txn("t-out", -50_000, name="TO SAVINGS"),
    _txn("t-in", 50_000, account="plaid-savings", name="FROM CHECKING", day="2026-07-20"),
]


async def test_synced_pair_gets_mirrored_detection_proposals(
    client, db, fake_provider, run_jobs
) -> None:
    """Opposite signs, equal magnitude, same currency, different accounts,
    inside the window → mirrored transfer proposals, provenance=detection,
    each naming the other."""
    await _signup(client)
    await _connect_and_sync(client, fake_provider, _batch(added=PAIR))
    await run_jobs()

    out_txn = await _one_txn(client, "TO SAVINGS")
    in_txn = await _one_txn(client, "FROM CHECKING")
    for side, other in ((out_txn, in_txn), (in_txn, out_txn)):
        proposal = side["proposal"]
        assert proposal is not None
        assert proposal["provenance"] == "detection"
        assert proposal["proposed_transfer"] is True
        assert proposal["counterpart_transaction_id"] == other["id"]
        assert proposal["category"] is None


async def test_ambiguity_proposes_nothing(client, db, fake_provider, run_jobs) -> None:
    """Two equal-amount candidates in the window → silence; a wrong link
    is worse than a missed one."""
    await _signup(client)
    await _connect_and_sync(
        client,
        fake_provider,
        _batch(
            added=[
                _txn("t-out", -50_000, name="TO SAVINGS"),
                _txn("t-in-1", 50_000, account="plaid-savings", name="CANDIDATE ONE"),
                _txn(
                    "t-in-2",
                    50_000,
                    account="plaid-savings",
                    name="CANDIDATE TWO",
                    day="2026-07-19",
                ),
            ]
        ),
    )
    await run_jobs()
    for t in await _listing(client):
        proposal = t["proposal"]
        assert proposal is None or proposal["provenance"] != "detection"


async def test_window_currency_account_and_zero_guards(client, db, fake_provider, run_jobs) -> None:
    """Outside ±5 days, cross-currency, same-account, and zero-amount
    pairs are never candidates."""
    await _signup(client)
    await _connect_and_sync(
        client,
        fake_provider,
        _batch(
            added=[
                _txn("t1", -50_000, name="STALE OUT", day="2026-07-01"),
                _txn("t2", 50_000, account="plaid-savings", name="STALE IN", day="2026-07-18"),
                _txn("t3", -7_000, name="EUR OUT", currency="EUR"),
                _txn("t4", 7_000, account="plaid-savings", name="USD IN"),
                _txn("t5", -3_000, name="SAME ACCT OUT"),
                _txn("t6", 3_000, name="SAME ACCT IN"),
                _txn("t7", 0, name="ZERO A"),
                _txn("t8", 0, account="plaid-savings", name="ZERO B"),
            ]
        ),
    )
    await run_jobs()
    for t in await _listing(client):
        proposal = t["proposal"]
        assert proposal is None or proposal["provenance"] != "detection", t["description_raw"]


async def _review(client, txn_id: str, body: dict | None = None):
    return await client.post(
        f"/api/v1/transactions/{txn_id}/review", json=body, headers=await _csrf(client)
    )


async def test_accepting_either_side_consumes_both(client, db, fake_provider, run_jobs) -> None:
    """One consent, one link: accepting one side creates the linked
    transfer, vacates both categories, reviews both sides, and logs the
    decision on both."""
    await _signup(client)
    await _connect_and_sync(client, fake_provider, _batch(added=PAIR))
    await run_jobs()
    out_txn = await _one_txn(client, "TO SAVINGS")
    in_txn = await _one_txn(client, "FROM CHECKING")

    response = await _review(client, out_txn["id"])  # accept-as-is
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "accepted"  # the shape proposed IS the shape decided

    transfers = (await client.get(TRANSFERS)).json()["items"]
    assert len(transfers) == 1
    out_after = await _one_txn(client, "TO SAVINGS")
    in_after = await _one_txn(client, "FROM CHECKING")
    for side in (out_after, in_after):
        assert side["transfer"] is not None
        assert side["reviewed_at"] is not None
        assert side["category"] is None
        assert side["proposal"] is None  # both consumed
    decisions = (await client.get(f"{CORRECTION_LOG}?kind=decision")).json()["items"]
    linked = [
        d
        for d in decisions
        if d["decision_transfer"] and d["decision_transfer"]["kind"] == "linked"
    ]
    assert {d["transaction_id"] for d in linked} == {out_txn["id"], in_txn["id"]}


async def test_batch_accept_consumes_detected_pair(client, db, fake_provider, run_jobs) -> None:
    await _signup(client)
    await _connect_and_sync(client, fake_provider, _batch(added=PAIR))
    await run_jobs()
    out_txn = await _one_txn(client, "TO SAVINGS")
    in_txn = await _one_txn(client, "FROM CHECKING")

    response = await client.post(
        "/api/v1/transactions/review",
        json={"ids": [out_txn["id"], in_txn["id"]]},
        headers=await _csrf(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == 1 and body["skipped"] == 1  # one consent covered both
    assert len((await client.get(TRANSFERS)).json()["items"]) == 1
    assert all(t["reviewed_at"] is not None for t in await _listing(client))


async def test_rejecting_one_side_kills_the_mirror(client, db, fake_provider, run_jobs) -> None:
    """Categorizing one side withdraws the counterpart's mirror — and the
    rejected pairing is remembered: the re-classify sweep must not
    re-propose it."""
    await _signup(client)
    await _connect_and_sync(client, fake_provider, _batch(added=PAIR))
    await run_jobs()
    out_txn = await _one_txn(client, "TO SAVINGS")

    category = await client.post(
        "/api/v1/categories", json={"name": "Fees"}, headers=await _csrf(client)
    )
    response = await _review(client, out_txn["id"], {"category_id": category.json()["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "corrected"
    await run_jobs()  # the deferred re-classification of the counterpart

    in_after = await _one_txn(client, "FROM CHECKING")
    assert in_after["reviewed_at"] is None
    proposal = in_after["proposal"]
    assert proposal is not None  # re-classified, not left proposal-less
    assert proposal["provenance"] != "detection"  # the pairing is declined memory
    assert proposal["proposed_transfer"] is False
    assert (await client.get(TRANSFERS)).json()["items"] == []


async def test_detects_against_reviewed_counterpart_and_accept_vacates(
    client, db, fake_provider, run_jobs
) -> None:
    """The Thursday case: one side synced and reviewed with a category
    days before the other arrives. The detector proposes on the new side;
    accepting vacates the reviewed counterpart's category while its
    reviewed state and original decision stand."""
    await _signup(client)
    body = await _connect_and_sync(
        client, fake_provider, _batch(added=[_txn("t-out", -50_000, name="CARD PAYMENT OUT")])
    )
    await run_jobs()
    out_txn = await _one_txn(client, "CARD PAYMENT OUT")
    category = await client.post(
        "/api/v1/categories", json={"name": "Card Payments"}, headers=await _csrf(client)
    )
    await _review(client, out_txn["id"], {"category_id": category.json()["id"]})
    reviewed_at = (await _one_txn(client, "CARD PAYMENT OUT"))["reviewed_at"]

    fake_provider.batches = [
        _batch(
            added=[
                _txn(
                    "t-in",
                    50_000,
                    account="plaid-savings",
                    name="CARD PAYMENT IN",
                    day="2026-07-20",
                )
            ]
        )
    ]
    refresh = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert refresh.status_code == 202
    await run_jobs()

    in_txn = await _one_txn(client, "CARD PAYMENT IN")
    proposal = in_txn["proposal"]
    assert proposal is not None and proposal["provenance"] == "detection"
    assert proposal["counterpart_transaction_id"] == out_txn["id"]
    # the reviewed side carries no proposal — it isn't in the inbox
    assert (await _one_txn(client, "CARD PAYMENT OUT"))["proposal"] is None

    response = await _review(client, in_txn["id"])
    assert response.status_code == 200, response.text
    out_after = await _one_txn(client, "CARD PAYMENT OUT")
    assert out_after["transfer"] is not None
    assert out_after["category"] is None  # vacated by the consented link
    assert out_after["reviewed_at"] == reviewed_at  # stays reviewed, untouched
    decisions = (await client.get(f"{CORRECTION_LOG}?kind=decision")).json()["items"]
    out_entries = [d for d in decisions if d["transaction_id"] == out_txn["id"]]
    assert len(out_entries) == 2  # the category decision stands; the link follows


async def test_detected_pair_outranks_untracked_rule_mark(
    client, db, fake_provider, run_jobs
) -> None:
    """A mark-transfer rule is right that it's a transfer; the detector
    merely knows more — the linked shape wins."""
    await _signup(client)
    rule = await client.post(
        "/api/v1/rules",
        json={
            "condition": {"payee": {"op": "contains", "value": "move money"}},
            "action_mark_transfer": True,
        },
        headers=await _csrf(client),
    )
    assert rule.status_code == 201, rule.text
    await _connect_and_sync(client, fake_provider, _batch(added=PAIR))
    await run_jobs()

    out_txn = await _one_txn(client, "TO SAVINGS")
    proposal = out_txn["proposal"]
    assert proposal is not None
    assert proposal["provenance"] == "detection"
    assert proposal["counterpart_transaction_id"] is not None  # linked, not untracked


async def test_amount_rewrite_invalidates_the_mirror(client, db, fake_provider, run_jobs) -> None:
    """CP3 meets CP4: rewriting one side's amount kills both sides'
    detection proposals — the stale mirror must not survive to vacate a
    category later."""
    await _signup(client)
    body = await _connect_and_sync(client, fake_provider, _batch(added=PAIR))
    await run_jobs()
    assert (await _one_txn(client, "FROM CHECKING"))["proposal"]["provenance"] == "detection"

    fake_provider.batches = [
        providers.SyncBatch(
            added=[],
            modified=[_txn("t-out", -49_000, name="TO SAVINGS")],
            removed=[],
            next_cursor="c-rewrite",
        )
    ]
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()

    out_after = await _one_txn(client, "TO SAVINGS")
    in_after = await _one_txn(client, "FROM CHECKING")
    for side in (out_after, in_after):
        proposal = side["proposal"]
        assert proposal is None or proposal["counterpart_transaction_id"] is None, side[
            "description_raw"
        ]


async def test_linked_and_split_rows_are_never_candidates(
    client, db, fake_provider, run_jobs
) -> None:
    """A row already in a transfer — or split — is out of the candidate
    pool on either side, even when amounts would match."""
    await _signup(client)
    await _connect_and_sync(
        client,
        fake_provider,
        _batch(
            added=[
                _txn("t-out", -50_000, name="TO SAVINGS"),
                _txn("t-in", 50_000, account="plaid-savings", name="FROM CHECKING"),
            ]
        ),
    )
    await run_jobs()
    out_txn = await _one_txn(client, "TO SAVINGS")
    # Hand-link the outflow as an untracked transfer: it leaves the pool.
    assert (
        await client.post(
            TRANSFERS, json={"transaction_ids": [out_txn["id"]]}, headers=await _csrf(client)
        )
    ).status_code == 201
    # Force a re-sweep; the linked row's twin must not re-propose against it.
    await client.post(
        "/api/v1/transactions/review",
        json={"ids": [out_txn["id"]]},
        headers=await _csrf(client),
    )
    await run_jobs()
    in_after = await _one_txn(client, "FROM CHECKING")
    proposal = in_after["proposal"]
    assert proposal is None or proposal["counterpart_transaction_id"] is None


async def test_manual_entry_pair_detected_via_same_job(client, db, run_jobs) -> None:
    """Manual creation funnels through the same classify job — detection
    is ingestion-path-agnostic (sync, import, manual alike)."""
    await _signup(client)
    accounts = {}
    for label in ("Checking", "Savings"):
        response = await client.post(
            "/api/v1/accounts",
            json={"kind": "depository", "label": label, "currency": "USD"},
            headers=await _csrf(client),
        )
        accounts[label] = response.json()["id"]
    for account_id, amount, name in (
        (accounts["Checking"], -25_000, "MANUAL OUT"),
        (accounts["Savings"], 25_000, "MANUAL IN"),
    ):
        response = await client.post(
            TRANSACTIONS,
            json={
                "account_id": account_id,
                "date": "2026-07-18",
                "amount_minor": amount,
                "description": name,
            },
            headers=await _csrf(client),
        )
        assert response.status_code == 201, response.text
    await run_jobs()

    out_txn = await _one_txn(client, "MANUAL OUT")
    assert out_txn["proposal"] is not None
    assert out_txn["proposal"]["provenance"] == "detection"
    assert (
        out_txn["proposal"]["counterpart_transaction_id"]
        == (await _one_txn(client, "MANUAL IN"))["id"]
    )
