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
        self.failure: providers.ProviderError | None = None
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
    await _signup(client)
    body = await _connect(client)
    await run_jobs()

    brokerage = next(a for a in body["accounts"] if a["kind"] == "investment")
    delete = await client.delete(f"{CONNECTIONS}/{body['id']}", headers=await _csrf(client))
    assert delete.status_code == 204, delete.text  # sever: accounts live on

    preview = (await client.get(f"/api/v1/accounts/{brokerage['id']}/deletion-preview")).json()
    assert preview["holdings"] == 2

    gone = await client.delete(f"/api/v1/accounts/{brokerage['id']}", headers=await _csrf(client))
    assert gone.status_code == 204, gone.text
    assert (await client.get(HOLDINGS)).json()["items"] == []

    from pinch_backend.models import Holding, Security

    assert await Holding.all() == []
    assert await Security.all() == []


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
