"""M10 CP0 seam: holdings over the public API (issue #73; PRD #72).

The fake provider scripts investments batches beside its cursor batches;
effects are asserted where a user or script would see them — the holdings
endpoint, the connection's health fields. ADR 0007's laws are proven
behaviorally: the investments phase never poisons the banking sync, never
runs for banking-only connections, and holdings are current-state.
"""

import uuid
from datetime import date

import pytest
from cryptography.fernet import Fernet

from pinch_backend import providers

CONNECTIONS = "/api/v1/connections"
HOLDINGS = "/api/v1/investments/holdings"
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


def _security(sid: str, name: str, ticker: str | None) -> providers.ProviderSecurity:
    return providers.ProviderSecurity(
        provider_security_id=sid,
        name=name,
        ticker_symbol=ticker,
        type="etf",
    )


def _holding(
    sid: str,
    quantity: float,
    value_minor: int,
    *,
    account: str = "plaid-brokerage",
    cost_basis_minor: int | None = None,
    price: float | None = None,
) -> providers.ProviderHolding:
    return providers.ProviderHolding(
        provider_account_id=account,
        provider_security_id=sid,
        quantity=quantity,
        institution_price=price,
        institution_price_as_of=date.fromisoformat("2026-07-18"),
        institution_value_minor=value_minor,
        cost_basis_minor=cost_basis_minor,
        currency="USD",
    )


def _batch(
    securities: list[providers.ProviderSecurity],
    holdings: list[providers.ProviderHolding],
) -> providers.InvestmentsBatch:
    return providers.InvestmentsBatch(securities=securities, holdings=holdings)


class FakeInvestmentsProvider:
    """The sync fake grown investments-shaped: scripted holdings batches
    beside the cursor batches, and a separate failure switch so the
    investments phase can break while banking stays healthy."""

    def __init__(self, *, with_brokerage: bool = True) -> None:
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
        if with_brokerage:
            self.accounts.append(
                providers.ProviderAccount(
                    provider_account_id="plaid-brokerage",
                    mask="7710",
                    name="Stash Brokerage",
                    kind="investment",
                    currency="USD",
                    balance_minor=500_000,
                )
            )
        self.batches: list[providers.SyncBatch] = []
        self.investment_batches: list[providers.InvestmentsBatch] = []
        self.activity_batches: list[providers.ActivitiesBatch] = []
        self.activity_windows: list[tuple] = []
        self.failure: providers.ProviderError | None = None
        self.transactions_failure: providers.ProviderError | None = None
        """Banking-only breakage: sync_transactions raises while
        get_accounts stays healthy — the stuck-Item shape."""
        self.investments_failure: providers.ProviderError | None = None
        self.holdings_calls = 0
        self.cursor_serial = 0

    async def create_link_token(self, *, client_user_id: str, access_token: str | None = None):
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
        if self.transactions_failure is not None:
            raise self.transactions_failure
        if self.batches:
            return self.batches.pop(0)
        self.cursor_serial += 1
        return providers.SyncBatch(
            added=[], modified=[], removed=[], next_cursor=f"cursor-{self.cursor_serial}"
        )

    async def get_holdings(self, access_token: str) -> providers.InvestmentsBatch:
        self.holdings_calls += 1
        if self.investments_failure is not None:
            raise self.investments_failure
        if self.investment_batches:
            return self.investment_batches.pop(0)
        return providers.InvestmentsBatch(securities=[], holdings=[])

    async def get_investment_activities(
        self, access_token: str, start_date: date, end_date: date
    ) -> providers.ActivitiesBatch:
        if self.investments_failure is not None:
            raise self.investments_failure
        self.activity_windows.append((start_date, end_date))
        if self.activity_batches:
            return self.activity_batches.pop(0)
        return providers.ActivitiesBatch(securities=[], activities=[])

    async def remove_item(self, access_token: str) -> None:
        return None

    async def get_institution_name(self, access_token: str) -> str | None:
        return "First Platypus Bank"


@pytest.fixture
def fake_provider(plaid_settings, monkeypatch):
    fake = FakeInvestmentsProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: fake)
    return fake


async def _connect(client) -> dict:
    response = await client.post(
        CONNECTIONS, json={"public_token": "public-abc"}, headers=await _csrf(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _default_batch() -> providers.InvestmentsBatch:
    return _batch(
        [
            _security("sec-aapl", "Apple Inc.", "AAPL"),
            _security("sec-vti", "Vanguard Total", "VTI"),
        ],
        [
            _holding("sec-aapl", 10.0, 1_900_00, cost_basis_minor=1_500_00, price=190.0),
            _holding("sec-vti", 4.5, 1_200_00, cost_basis_minor=1_100_00, price=266.67),
        ],
    )


async def test_sync_lands_holdings_with_embedded_security(client, db, fake_provider, run_jobs):
    """The CP0 story: connect an investment-bearing institution, sync, and
    the positions are readable — security identity embedded, money in minor
    units, quantity and cost basis intact."""
    fake_provider.investment_batches = [_default_batch()]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    page = (await client.get(HOLDINGS)).json()
    assert set(page) == {"items", "next_cursor"}
    items = {h["security"]["ticker_symbol"]: h for h in page["items"]}
    assert set(items) == {"AAPL", "VTI"}
    aapl = items["AAPL"]
    assert aapl["security"]["name"] == "Apple Inc."
    assert aapl["quantity"] == 10.0
    assert aapl["institution_value_minor"] == 1_900_00
    assert aapl["cost_basis_minor"] == 1_500_00
    assert aapl["currency"] == "USD"

    brokerage = next(a for a in body["accounts"] if a["kind"] == "investment")
    filtered = (await client.get(HOLDINGS, params={"account_id": brokerage["id"]})).json()
    assert len(filtered["items"]) == 2
    other = next(a for a in body["accounts"] if a["kind"] == "depository")
    assert (await client.get(HOLDINGS, params={"account_id": other["id"]})).json()["items"] == []


async def test_banking_only_connection_never_calls_investments(
    client, db, plaid_settings, monkeypatch, run_jobs
):
    """The billing gate (PRD #72): no investment-kind accounts, no
    investments call — cost surface equals value surface."""
    fake = FakeInvestmentsProvider(with_brokerage=False)
    monkeypatch.setattr(providers, "get_provider", lambda: fake)

    await _signup(client)
    await _connect(client)
    await run_jobs()
    assert fake.holdings_calls == 0
    assert (await client.get(HOLDINGS)).json()["items"] == []


async def test_investments_failure_leaves_banking_commit_standing(
    client, db, fake_provider, run_jobs
):
    """Failure isolation (ADR 0007): the investments phase breaking records
    an investments error on the connection and nothing else — balances land,
    status stays active, no retry poisons the banking pass."""
    fake_provider.investments_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="not yet extracted"
    )
    fake_provider.batches = [
        providers.SyncBatch(
            added=[
                providers.ProviderTransaction(
                    provider_transaction_id="t1",
                    provider_account_id="plaid-checking",
                    amount_minor=-1234,
                    currency="USD",
                    date=date.fromisoformat("2026-07-18"),
                    description="COFFEE SHOP",
                    pending=False,
                )
            ],
            modified=[],
            removed=[],
            next_cursor="cursor-1",
        )
    ]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    listing = (await client.get(TRANSACTIONS)).json()["items"]
    assert {t["description_raw"] for t in listing} == {"COFFEE SHOP"}
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"
    assert health["error_detail"] is None
    assert health["investments_error_detail"] == "PRODUCT_NOT_READY"
    assert (await client.get(HOLDINGS)).json()["items"] == []


async def test_investments_error_heals_on_next_sync(client, db, fake_provider, run_jobs):
    fake_provider.investments_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="not yet extracted"
    )
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).json()[
        "investments_error_detail"
    ] == "PRODUCT_NOT_READY"

    fake_provider.investments_failure = None
    fake_provider.investment_batches = [_default_batch()]
    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 202, response.text
    await run_jobs()

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["investments_error_detail"] is None
    assert len((await client.get(HOLDINGS)).json()["items"]) == 2


async def test_holdings_are_current_state_replaced(client, db, fake_provider, run_jobs):
    """The mirror law for holdings: a re-sync updates positions in place
    and a sold-out position disappears — no duplicate securities minted."""
    fake_provider.investment_batches = [_default_batch()]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    assert len((await client.get(HOLDINGS)).json()["items"]) == 2

    fake_provider.investment_batches = [
        _batch(
            [_security("sec-aapl", "Apple Inc.", "AAPL")],
            [_holding("sec-aapl", 12.0, 2_280_00, cost_basis_minor=1_800_00, price=190.0)],
        )
    ]
    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 202, response.text
    await run_jobs()

    items = (await client.get(HOLDINGS)).json()["items"]
    assert len(items) == 1
    assert items[0]["security"]["ticker_symbol"] == "AAPL"
    assert items[0]["quantity"] == 12.0
    assert items[0]["institution_value_minor"] == 2_280_00

    from pinch_backend.models import Security

    assert len(await Security.all()) == 2  # VTI's identity survives; no dup AAPL


async def test_hard_delete_cascades_holdings_and_orphan_securities(
    client, db, fake_provider, run_jobs
):
    """Hard delete stays complete (#71 extended): a disconnected investment
    account takes its holdings, and securities nothing holds anymore, with
    it. The deletion preview states the holdings count."""
    fake_provider.investment_batches = [_default_batch()]
    fake_provider.activity_batches = [
        _activities(_activity("act-buy", "2026-07-10", -77_00, "buy"))
    ]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    brokerage = next(a for a in body["accounts"] if a["kind"] == "investment")
    delete = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert delete.status_code == 204, delete.text  # sever: accounts live on

    preview = (await client.get(f"/api/v1/accounts/{brokerage['id']}/deletion-preview")).json()
    assert preview["holdings"] == 2
    assert preview["investment_activities"] == 1

    gone = await client.delete(f"/api/v1/accounts/{brokerage['id']}", headers=await _csrf(client))
    assert gone.status_code == 204, gone.text
    assert (await client.get(HOLDINGS)).json()["items"] == []
    assert (await client.get(ACTIVITIES)).json()["items"] == []

    from pinch_backend.models import Holding, InvestmentActivity, Security

    assert await Holding.all() == []
    assert await InvestmentActivity.all() == []
    assert await Security.all() == []


def _activity(
    aid: str,
    day: str,
    amount_minor: int,
    activity_type: str,
    *,
    sid: str | None = "sec-aapl",
    account: str = "plaid-brokerage",
    name: str = "BUY AAPL",
    quantity: float = 0.0,
    subtype: str | None = None,
) -> providers.ProviderInvestmentActivity:
    return providers.ProviderInvestmentActivity(
        provider_activity_id=aid,
        provider_account_id=account,
        provider_security_id=sid,
        date=date.fromisoformat(day),
        name=name,
        amount_minor=amount_minor,
        quantity=quantity,
        price=None,
        fees_minor=None,
        type=activity_type,
        subtype=subtype,
        currency="USD",
    )


def _activities(*activities, securities=None) -> providers.ActivitiesBatch:
    return providers.ActivitiesBatch(
        securities=securities
        if securities is not None
        else [_security("sec-aapl", "Apple Inc.", "AAPL")],
        activities=list(activities),
    )


ACTIVITIES = "/api/v1/investments/activities"


async def test_sync_lands_the_activity_feed(client, db, fake_provider, run_jobs):
    """The CP1 story: buys, dividends, and even cancel rows land as
    activities — newest first, security identity embedded where one is
    named, amounts signed from the account's perspective."""
    fake_provider.activity_batches = [
        _activities(
            _activity("act-buy", "2026-07-10", -77_00, "buy", name="BUY AAPL", quantity=0.4),
            _activity(
                "act-div",
                "2026-07-15",
                8_72,
                "cash",
                name="AAPL DIVIDEND",
                subtype="dividend",
            ),
            _activity("act-cancel", "2026-07-01", 0, "cancel", sid=None, name="CANCELLED TRADE"),
        )
    ]
    await _signup(client)
    await _connect(client)
    await run_jobs()

    page = (await client.get(ACTIVITIES)).json()
    assert set(page) == {"items", "next_cursor"}
    assert [a["name"] for a in page["items"]] == [
        "AAPL DIVIDEND",  # newest first
        "BUY AAPL",
        "CANCELLED TRADE",
    ]
    by_id = {a["name"]: a for a in page["items"]}
    buy = by_id["BUY AAPL"]
    assert buy["amount_minor"] == -77_00  # money out of the account
    assert buy["type"] == "buy"
    assert buy["quantity"] == 0.4
    assert buy["security"]["ticker_symbol"] == "AAPL"
    dividend = by_id["AAPL DIVIDEND"]
    assert dividend["amount_minor"] == 8_72  # money in
    assert dividend["subtype"] == "dividend"
    cancel = by_id["CANCELLED TRADE"]
    assert cancel["type"] == "cancel"
    assert cancel["security"] is None

    # The requested window is Plaid's 24-month cap, ending today.
    (start, end), *_ = fake_provider.activity_windows
    assert (end - start).days == 730


async def test_activities_mirror_with_retention_floor(client, db, fake_provider, run_jobs):
    """Stateless mirror (ADR 0007): an in-window activity Plaid stops
    returning disappears; one older than the window start survives every
    sync — captured history is kept forever."""
    fake_provider.activity_batches = [
        _activities(
            _activity("act-recent", "2026-07-10", -50_00, "buy"),
            _activity("act-ancient", "2023-01-15", -25_00, "buy", name="ANCIENT BUY"),
        )
    ]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    assert len((await client.get(ACTIVITIES)).json()["items"]) == 2

    fake_provider.activity_batches = [_activities()]  # empty window now
    response = await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    assert response.status_code == 202
    await run_jobs()

    items = (await client.get(ACTIVITIES)).json()["items"]
    assert [a["name"] for a in items] == ["ANCIENT BUY"]  # the floor held


async def test_unknown_activity_type_shields_its_stored_row_from_the_sweep(
    client, db, fake_provider, run_jobs
):
    """Vocabulary this build doesn't know skips loudly — but the mirror
    must not doom the stored row: Plaid is still returning it. The row
    survives with its last-known shape."""
    fake_provider.activity_batches = [
        _activities(_activity("act-1", "2026-07-10", -50_00, "buy", name="KNOWN BUY"))
    ]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    assert len((await client.get(ACTIVITIES)).json()["items"]) == 1

    fake_provider.activity_batches = [
        _activities(
            _activity("act-1", "2026-07-10", -50_00, "adjustment", name="KNOWN BUY")
        )  # Plaid re-types the same row with vocabulary we don't know
    ]
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()

    items = (await client.get(ACTIVITIES)).json()["items"]
    assert [a["name"] for a in items] == ["KNOWN BUY"]  # survived, last-known shape
    assert items[0]["type"] == "buy"


async def test_resyncing_upserts_in_place_without_duplicates(client, db, fake_provider, run_jobs):
    """The update branch must actually apply — asserting a count alone
    survives a crashed second sync (the exact hole the smoke test found:
    relation assignment on a fetched row raises, the job silently
    retried, and count-stable dedupe stayed green)."""
    fake_provider.activity_batches = [
        _activities(
            _activity("act-1", "2026-07-10", -50_00, "buy"),
            _activity("act-2", "2026-07-11", 8_72, "cash", subtype="dividend"),
        )
    ]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    fake_provider.activity_batches = [
        _activities(
            # Same provider ids — one amended by Plaid, still naming its
            # security: the update branch, relation and all.
            _activity("act-1", "2026-07-10", -55_00, "buy", name="BUY AAPL AMENDED"),
            _activity("act-2", "2026-07-11", 8_72, "cash", subtype="dividend"),
        )
    ]
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()

    items = (await client.get(ACTIVITIES)).json()["items"]
    assert len(items) == 2  # no duplicates...
    amended = next(a for a in items if a["name"] == "BUY AAPL AMENDED")
    assert amended["amount_minor"] == -55_00  # ...and the amendment landed
    assert amended["security"]["ticker_symbol"] == "AAPL"

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["investments_error_detail"] is None


async def test_a_code_bug_in_the_phase_records_and_never_retries(
    client, db, fake_provider, run_jobs
):
    """Isolation covers our own bugs too: a non-provider exception rolls
    back the phase, lands as INTERNAL_ERROR, and the job still succeeds —
    the committed banking pass is never replayed (smoke-test finding)."""
    fake_provider.investments_failure = RuntimeError("boom")  # type: ignore[assignment]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "active"  # banking untouched, job succeeded
    assert health["investments_error_detail"] == "INTERNAL_ERROR"
    assert (await client.get(HOLDINGS)).json()["items"] == []


async def test_orphan_sweep_spares_securities_still_named_by_activities(
    client, db, fake_provider, run_jobs
):
    """Hard delete's security sweep counts activities as holders too: a
    security whose only remaining reference is another account's activity
    survives."""
    fake_provider.accounts.append(
        providers.ProviderAccount(
            provider_account_id="plaid-ira",
            mask="9001",
            name="Stash IRA",
            kind="investment",
            currency="USD",
            balance_minor=200_000,
        )
    )
    fake_provider.investment_batches = [
        _batch(
            [_security("sec-aapl", "Apple Inc.", "AAPL")],
            [_holding("sec-aapl", 10.0, 1_900_00)],  # holding on plaid-brokerage only
        )
    ]
    fake_provider.activity_batches = [
        _activities(
            _activity(
                "act-vti-buy",
                "2026-07-10",
                -50_00,
                "buy",
                sid="sec-vti",
                account="plaid-ira",
                name="IRA VTI BUY",
            ),
            securities=[_security("sec-vti", "Vanguard Total", "VTI")],
        )
    ]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    brokerage = next(
        a for a in body["accounts"] if a["kind"] == "investment" and a["label"] == "Stash Brokerage"
    )
    delete = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert delete.status_code == 204
    gone = await client.delete(f"/api/v1/accounts/{brokerage['id']}", headers=await _csrf(client))
    assert gone.status_code == 204, gone.text

    from pinch_backend.models import Security

    survivors = {s.ticker_symbol for s in await Security.all()}
    assert survivors == {"VTI"}  # AAPL orphaned and swept; VTI held by the IRA's activity
    assert [a["name"] for a in (await client.get(ACTIVITIES)).json()["items"]] == ["IRA VTI BUY"]


async def test_missing_consent_flags_connection_and_banking_stands(
    client, db, fake_provider, run_jobs
):
    """M10 CP2 (issue #75): the consent posture is a sibling of reauth,
    not an error — the flag rises, error detail stays clean, the
    connection stays active, and banking data lands untouched."""
    fake_provider.investments_failure = providers.ProviderError(
        code="ADDITIONAL_CONSENT_REQUIRED", message="consent needed"
    )
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["investments_consent_required"] is True
    assert health["investments_error_detail"] is None  # consent isn't an error
    assert health["status"] == "active"
    assert (await client.get(HOLDINGS)).json()["items"] == []

    brokerage = next(a for a in body["accounts"] if a["kind"] == "investment")
    entries = (await client.get(f"/api/v1/accounts/{brokerage['id']}/balance-entries")).json()
    assert len(entries["items"]) == 1  # the banking phase committed


async def test_consent_flag_clears_after_consented_resync(client, db, fake_provider, run_jobs):
    """The retrofit story: update-mode Link collects consent out-of-band;
    the next sync simply succeeds and the flag falls."""
    fake_provider.investments_failure = providers.ProviderError(
        code="ADDITIONAL_CONSENT_REQUIRED", message="consent needed"
    )
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    assert (await client.get(f"{CONNECTIONS}/{body['id']}")).json()[
        "investments_consent_required"
    ] is True

    fake_provider.investments_failure = None  # consent granted via update-mode Link
    fake_provider.investment_batches = [_default_batch()]
    fake_provider.activity_batches = [
        _activities(_activity("act-buy", "2026-07-10", -77_00, "buy"))
    ]
    await client.post(f"{CONNECTIONS}/{body['id']}/sync", headers=await _csrf(client))
    await run_jobs()

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["investments_consent_required"] is False
    assert len((await client.get(HOLDINGS)).json()["items"]) == 2
    assert len((await client.get(ACTIVITIES)).json()["items"]) == 1  # activities land too


async def test_creation_consented_connection_never_flags(client, db, fake_provider, run_jobs):
    """A connection that consented at creation sails straight through —
    the flag never rises."""
    fake_provider.investment_batches = [_default_batch()]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["investments_consent_required"] is False
    assert health["investments_error_detail"] is None


async def test_non_consent_investments_errors_still_record_as_errors(
    client, db, fake_provider, run_jobs
):
    """The consent set is narrow: any other investments failure stays an
    investments error, and the consent flag stays down."""
    fake_provider.investments_failure = providers.ProviderError(
        code="INTERNAL_SERVER_ERROR", message="plaid hiccup"
    )
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["investments_consent_required"] is False
    assert health["investments_error_detail"] == "INTERNAL_SERVER_ERROR"


async def test_stuck_banking_still_raises_the_consent_flag(client, db, fake_provider):
    """Bidirectional isolation (post-QA fix): a transactions product
    perpetually PRODUCT_NOT_READY — the real Stash shape — must not hold
    investments hostage. At ladder exhaustion the holdings call still
    fires, so the consent flag can rise while banking stays stuck."""
    from pinch_backend.sync import run_sync

    fake_provider.transactions_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="initial transaction pull not finished"
    )
    fake_provider.investments_failure = providers.ProviderError(
        code="ADDITIONAL_CONSENT_REQUIRED", message="consent needed"
    )
    await _signup(client)
    body = await _connect(client)
    await run_sync(uuid.UUID(body["id"]), final_attempt=True)

    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "error"
    assert health["error_detail"] == "PRODUCT_NOT_READY"
    assert health["investments_consent_required"] is True


async def test_stuck_banking_still_lands_holdings(client, db, fake_provider):
    """The same shape with investments healthy: positions land even while
    the banking product never becomes ready."""
    from pinch_backend.sync import run_sync

    fake_provider.transactions_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="initial transaction pull not finished"
    )
    fake_provider.investment_batches = [_default_batch()]
    await _signup(client)
    body = await _connect(client)
    await run_sync(uuid.UUID(body["id"]), final_attempt=True)

    assert len((await client.get(HOLDINGS)).json()["items"]) == 2
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "error"  # banking honesty is untouched


async def test_transient_banking_error_defers_investments_to_ladder_end(client, db, fake_provider):
    """Retries with attempts remaining re-raise without touching
    investments — one holdings call per ladder, at exhaustion, never five."""
    import pytest as _pytest

    from pinch_backend.sync import run_sync

    fake_provider.transactions_failure = providers.ProviderError(
        code="PRODUCT_NOT_READY", message="initial transaction pull not finished"
    )
    await _signup(client)
    body = await _connect(client)
    with _pytest.raises(providers.ProviderError):
        await run_sync(uuid.UUID(body["id"]), final_attempt=False)
    assert fake_provider.holdings_calls == 0


async def test_auth_error_skips_investments_entirely(client, db, fake_provider):
    """A dead login is dead for both products: reauth-required stops the
    pass before any investments call."""
    from pinch_backend.sync import run_sync

    fake_provider.transactions_failure = providers.ProviderError(
        code="ITEM_LOGIN_REQUIRED", message="login is dead"
    )
    await _signup(client)
    body = await _connect(client)
    await run_sync(uuid.UUID(body["id"]), final_attempt=True)

    assert fake_provider.holdings_calls == 0
    health = (await client.get(f"{CONNECTIONS}/{body['id']}")).json()
    assert health["status"] == "reauth_required"
    assert health["investments_consent_required"] is False


async def test_holdings_are_ledger_fenced(client, db, fake_provider, run_jobs):
    """Tenancy: another ledger's holdings are invisible — the list is
    fenced, and a foreign account filter answers an empty page, never a
    confirming error."""
    fake_provider.investment_batches = [_default_batch()]
    await _signup(client)
    body = await _connect(client)
    await run_jobs()
    brokerage_id = next(a["id"] for a in body["accounts"] if a["kind"] == "investment")

    other = await client.post(
        "/api/v1/auth/signup",
        json={"email": "other@example.com", "password": PASSWORD, "display_name": "Other"},
        headers=await _csrf(client),
    )
    assert other.status_code == 201
    assert (await client.get(HOLDINGS)).json()["items"] == []
    assert (await client.get(HOLDINGS, params={"account_id": brokerage_id})).json()["items"] == []
    assert uuid.UUID(brokerage_id)  # sanity: the filter really was a real id
