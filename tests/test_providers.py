"""The owned provider clients against their wire shapes (M7 CP1, issue
#33; provider-neutral reshape M13 CP1, #88; MX M13 CP2, #89).

httpx.MockTransport scripts each provider's captured JSON — Plaid's
documented shapes, MX's CP0-spike-captured ones (docs/research/
mx-platform-api.md) — so kind mapping, money conversion, and error
surfacing are proven without a network; the opt-in live smokes prove the
same clients against the real things. Credentials ride the constructors
since M13 CP1: Plaid binds an access token, MX binds guids, and the
connect pair is the only pre-credential surface.
"""

import base64
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from pinch_backend.providers import (
    BACKFILL_DAYS,
    MX_INITIAL_WINDOW_DAYS,
    MX_REMOVAL_WINDOW_DAYS,
    MX_SYNC_LOOKAHEAD_DAYS,
    MXProvider,
    PlaidProvider,
    ProviderError,
)


def _provider(handler, access_token: str | None = None) -> PlaidProvider:
    return PlaidProvider(
        client_id="cid",
        secret="sec",
        environment="sandbox",
        access_token=access_token,
        transport=httpx.MockTransport(handler),
    )


async def test_connect_session_carries_backfill_and_credentials() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"link_token": "link-sandbox-123"})

    token = await _provider(handler).create_connect_session(client_user_id="user-1")
    assert token == "link-sandbox-123"
    assert seen["path"] == "/link/token/create"
    assert seen["client_id"] == "cid" and seen["secret"] == "sec"
    assert seen["user"] == {"client_user_id": "user-1"}
    assert seen["transactions"] == {"days_requested": BACKFILL_DAYS}
    assert BACKFILL_DAYS == 730  # PRD #31: two years of history requested


def _exchange_flow_handler(request: httpx.Request) -> httpx.Response:
    """The complete_connect wire flow (M13 CP1): exchange, then the
    institution identity in the same motion."""
    if request.url.path == "/item/public_token/exchange":
        return httpx.Response(200, json={"access_token": "access-sandbox-x", "item_id": "item-x"})
    if request.url.path == "/item/get":
        return httpx.Response(200, json={"item": {"institution_id": "ins_1"}})
    assert request.url.path == "/institutions/get_by_id"
    return httpx.Response(200, json={"institution": {"name": "First Platypus Bank"}})


async def test_complete_connect_parses_identity_and_secret() -> None:
    """The exchange and the institution identity land together: item id,
    institution id + name (the dupe guard's basis), and the minted access
    token as the connection's secret."""
    result = await _provider(_exchange_flow_handler).complete_connect("public-x")
    assert result.provider_item_id == "item-x"
    assert result.provider_institution_id == "ins_1"
    assert result.institution_name == "First Platypus Bank"
    assert result.secret is not None
    # SecretStr: the token never rides a repr — only an explicit unwrap.
    assert result.secret.get_secret_value() == "access-sandbox-x"
    assert "access-sandbox-x" not in repr(result)


async def test_complete_connect_binds_the_instance() -> None:
    """The minted token binds the instance: the caller's next verbs
    (get_accounts) ride the same materialization without re-handling the
    secret."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            assert json.loads(request.content)["access_token"] == "access-sandbox-x"
            return httpx.Response(200, json={"accounts": []})
        return _exchange_flow_handler(request)

    provider = _provider(handler)
    await provider.complete_connect("public-x")
    assert await provider.get_accounts() == []


async def test_complete_connect_tolerates_institution_lookup_failure() -> None:
    """The institution identity is a nicety — its lookup failing must
    never block a consented connect; sync backfills opportunistically."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/item/public_token/exchange":
            return httpx.Response(
                200, json={"access_token": "access-sandbox-x", "item_id": "item-x"}
            )
        raise httpx.ConnectError("connection refused")

    result = await _provider(handler).complete_connect("public-x")
    assert result.provider_item_id == "item-x"
    assert result.provider_institution_id is None and result.institution_name is None
    assert result.secret is not None
    assert result.secret.get_secret_value() == "access-sandbox-x"


async def test_accounts_map_kinds_and_currency() -> None:
    """Plaid's five types land on Pinch kinds; `other` is the asset
    catch-all; a silent currency stays None for the caller's fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "account_id": "a1",
                        "name": "Checking",
                        "type": "depository",
                        "balances": {"iso_currency_code": "USD"},
                    },
                    {
                        "account_id": "a2",
                        "name": "Card",
                        "type": "credit",
                        "balances": {"iso_currency_code": "USD"},
                    },
                    {"account_id": "a3", "name": "Mortgage", "type": "loan", "balances": {}},
                    {
                        "account_id": "a4",
                        "name": "Brokerage",
                        "type": "investment",
                        "balances": {"iso_currency_code": "USD"},
                    },
                    {
                        "account_id": "a5",
                        "name": "Mystery",
                        "type": "other",
                        "balances": {"iso_currency_code": None},
                    },
                ]
            },
        )

    accounts = await _provider(handler, "access-x").get_accounts()
    kinds = {a.provider_account_id: a.kind.value for a in accounts}
    assert kinds == {
        "a1": "depository",
        "a2": "credit",
        "a3": "loan",
        "a4": "investment",
        "a5": "asset",
    }
    currencies = {a.provider_account_id: a.currency for a in accounts}
    assert currencies["a1"] == "USD"
    assert currencies["a3"] is None and currencies["a5"] is None


async def test_remove_item_posts_token() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"removed": True})

    await _provider(handler, "access-x").remove_item()
    assert seen["path"] == "/item/remove"
    assert seen["access_token"] == "access-x"


async def test_update_mode_connect_session_carries_access_token() -> None:
    """Reauth repair (PRD #31): the same endpoint in update mode — a
    bound instance (M13 CP1: the mode is the binding) sends the Item's
    access token instead of a products request."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-update-1"})

    token = await _provider(handler, "access-broken").create_connect_session(
        client_user_id="user-1"
    )
    assert token == "link-update-1"
    assert seen["access_token"] == "access-broken"
    assert "products" not in seen  # update mode repairs; it doesn't re-request


async def test_creation_link_token_carries_the_webhook_url(monkeypatch) -> None:
    """New Items are born registered (M11 CP0, PRD #77): creation mode
    carries the instance's webhook URL so Plaid can ring from day one."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_webhook_url", "https://pinch.example/webhooks/plaid")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-sandbox-123"})

    await _provider(handler).create_connect_session(client_user_id="user-1")
    assert seen["webhook"] == "https://pinch.example/webhooks/plaid"


async def test_update_mode_connect_session_carries_no_webhook_url(monkeypatch) -> None:
    """Update mode is unchanged (PRD #77 decision 8): registration lives at
    creation and in the reconciler, never on the repair path."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_webhook_url", "https://pinch.example/webhooks/plaid")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-update-1"})

    await _provider(handler, "access-broken").create_connect_session(client_user_id="user-1")
    assert "webhook" not in seen


async def test_update_webhook_posts_token_and_url() -> None:
    """The reconciler's re-registration call (M11 CP3): /item/webhook/update
    with the Item's token and the instance's receiver URL."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"item": {}, "request_id": "req-1"})

    await _provider(handler, "access-x").update_webhook("https://pinch.example/webhooks/plaid")
    assert seen["path"] == "/item/webhook/update"
    assert seen["access_token"] == "access-x"
    assert seen["webhook"] == "https://pinch.example/webhooks/plaid"


async def test_item_state_reads_webhook_and_status_timestamps() -> None:
    """The reconciler's one free probe (M11 CP3): a plain /item/get —
    item.webhook plus Plaid's own last-successful-update stamps (the
    production-empirical shape: status rides top-level, no parameters)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "item": {"item_id": "item-x", "webhook": "https://old.example/hook"},
                "status": {
                    "transactions": {"last_successful_update": "2026-08-01T10:20:30Z"},
                    "investments": {"last_successful_update": None},
                    "last_webhook": None,
                },
                "request_id": "req-1",
            },
        )

    state = await _provider(handler, "access-x").get_item_state()
    assert seen["path"] == "/item/get"
    assert seen["access_token"] == "access-x"
    assert state.webhook == "https://old.example/hook"
    assert state.transactions_updated_at is not None
    assert state.transactions_updated_at.isoformat() == "2026-08-01T10:20:30+00:00"
    assert state.investments_updated_at is None


async def test_item_state_reads_an_unregistered_item_as_empty_webhook() -> None:
    """Pre-M11 Items carry webhook '' (production-empirical) — the shape
    the retrofit verdict keys on; a missing status never crashes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"item": {"item_id": "item-x", "webhook": ""}, "request_id": "req-1"}
        )

    state = await _provider(handler, "access-x").get_item_state()
    assert state.webhook == ""
    assert state.transactions_updated_at is None and state.investments_updated_at is None


async def test_webhook_verification_key_fetch_posts_the_key_id() -> None:
    """The JWT verifier resolves Plaid's signing key by kid through the
    provider seam (M11 CP0) — fakeable exactly like every provider call."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "key": {
                    "alg": "ES256",
                    "crv": "P-256",
                    "kid": "kid-1",
                    "kty": "EC",
                    "use": "sig",
                    "x": "35lvC8uz2QrWpQJ3TUYk5pQJ983FL2uBiwEfxlHQtQw",
                    "y": "I7Qqrz1wcQvczkPUJKUCzII0K18Ao81xM-BHVf_7wpE",
                },
                "request_id": "req-1",
            },
        )

    key = await _provider(handler).get_webhook_verification_key("kid-1")
    assert seen["path"] == "/webhook_verification_key/get"
    assert seen["client_id"] == "cid" and seen["secret"] == "sec"
    assert seen["key_id"] == "kid-1"
    assert key["kid"] == "kid-1" and key["crv"] == "P-256"


async def test_accounts_carry_minor_unit_balances() -> None:
    """Plaid reports floats in major units; Pinch speaks integer minor
    units (CONTEXT.md: Amount) — exponent-aware, never naive *100."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "account_id": "a1",
                        "name": "Checking",
                        "type": "depository",
                        "balances": {"iso_currency_code": "USD", "current": 1234.56},
                    },
                    {
                        "account_id": "a2",
                        "name": "Yen",
                        "type": "depository",
                        "balances": {"iso_currency_code": "JPY", "current": 5000},
                    },
                    {
                        "account_id": "a3",
                        "name": "Silent",
                        "type": "depository",
                        "balances": {"iso_currency_code": "USD", "current": None},
                    },
                ]
            },
        )

    accounts = await _provider(handler, "access-x").get_accounts()
    balances = {a.provider_account_id: a.balance_minor for a in accounts}
    assert balances == {"a1": 123456, "a2": 5000, "a3": None}


async def test_sync_paginates_and_converts() -> None:
    """The cursor loop drains has_more pages in one call; Plaid's
    positive-is-debit floats become Pinch's negative-is-out minor units."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/transactions/sync"
        calls.append(body.get("cursor"))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "added": [
                        {
                            "transaction_id": "t1",
                            "account_id": "a1",
                            "amount": 12.34,
                            "iso_currency_code": "USD",
                            "date": "2026-07-18",
                            "name": "COFFEE SHOP",
                            "pending": True,
                            "pending_transaction_id": None,
                        }
                    ],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-page-2",
                    "has_more": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "added": [
                    {
                        "transaction_id": "t2",
                        "account_id": "a1",
                        "amount": -250.00,
                        "iso_currency_code": "USD",
                        "date": "2026-07-17",
                        "name": "PAYCHECK",
                        "pending": False,
                        "pending_transaction_id": None,
                    }
                ],
                "modified": [
                    {
                        "transaction_id": "t3",
                        "account_id": "a1",
                        "amount": 40.00,
                        "iso_currency_code": "USD",
                        "date": "2026-07-16",
                        "name": "GAS STATION",
                        "pending": False,
                        "pending_transaction_id": "t-pending-3",
                    }
                ],
                "removed": [{"transaction_id": "t-gone"}],
                "next_cursor": "cursor-final",
                "has_more": False,
            },
        )

    batch = await _provider(handler, "access-x").sync_transactions(cursor="cursor-start")
    assert calls == ["cursor-start", "cursor-page-2"]
    assert batch.next_cursor == "cursor-final"
    assert [t.provider_transaction_id for t in batch.added] == ["t1", "t2"]
    coffee, paycheck = batch.added
    assert coffee.amount_minor == -1234  # 12.34 debit → -1234 minor
    assert coffee.pending is True
    assert coffee.provider_account_id == "a1"
    assert paycheck.amount_minor == 25000  # -250.00 credit → +25000
    (gas,) = batch.modified
    assert gas.pending_provider_transaction_id == "t-pending-3"
    assert batch.removed == ["t-gone"]


async def test_sync_not_ready_surfaces_transient_error() -> None:
    """A fresh Item mid-initial-pull answers empty-with-empty-cursor
    (live-sandbox finding): that cursor must never persist — it surfaces
    as a transient PRODUCT_NOT_READY into the retry ladder instead.
    With or without Plaid saying NOT_READY explicitly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "",
                "has_more": False,
                "transactions_update_status": "NOT_READY",
            },
        )

    with pytest.raises(ProviderError) as excinfo:
        await _provider(handler, "access-x").sync_transactions(cursor=None)
    assert excinfo.value.code == "PRODUCT_NOT_READY"

    def legacy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"added": [], "modified": [], "removed": [], "next_cursor": "", "has_more": False},
        )

    with pytest.raises(ProviderError) as excinfo:
        await _provider(legacy_handler, "access-x").sync_transactions(cursor=None)
    assert excinfo.value.code == "PRODUCT_NOT_READY"


async def test_sync_accepts_a_completed_zero_transaction_item() -> None:
    """The Stash probe finding (M10): an Item whose accounts simply have
    no transactions answers empty-with-empty-cursor too — but with
    transactions_update_status complete. That's a real empty ledger, not
    a pull in progress; the batch lands and the empty cursor persists."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "cursor" not in body  # "" replays as a fresh start, never sent
        return httpx.Response(
            200,
            json={
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "",
                "has_more": False,
                "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            },
        )

    provider = _provider(handler, "access-x")
    batch = await provider.sync_transactions(cursor=None)
    assert batch.added == [] and batch.next_cursor == ""
    # The persisted "" cursor round-trips as a fresh start next sync.
    batch = await provider.sync_transactions(cursor="")
    assert batch.next_cursor == ""


async def test_sync_initial_cursor_omitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "cursor" not in body
        return httpx.Response(
            200,
            json={
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "c1",
                "has_more": False,
            },
        )

    batch = await _provider(handler, "access-x").sync_transactions(cursor=None)
    assert batch.next_cursor == "c1"


async def test_transport_fault_becomes_provider_error() -> None:
    """The archetypal institution-down shapes — timeouts, resets, DNS —
    must land inside the error contract, never escape it raw."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ProviderError) as excinfo:
        await _provider(handler, "access-x").get_accounts()
    assert excinfo.value.code == "NETWORK_ERROR"


async def test_non_json_response_becomes_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    with pytest.raises(ProviderError) as excinfo:
        await _provider(handler, "access-x").get_accounts()
    assert excinfo.value.code == "HTTP_502"


async def test_plaid_error_surfaces_code_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_code": "INVALID_PUBLIC_TOKEN",
                "error_message": "provided public token is expired",
            },
        )

    with pytest.raises(ProviderError) as excinfo:
        await _provider(handler).complete_connect("public-stale")
    assert excinfo.value.code == "INVALID_PUBLIC_TOKEN"


async def test_accounts_carry_mask() -> None:
    """Plaid's mask (last 2-4 digits) rides along; absent stays None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "account_id": "acc-1",
                        "name": "Checking",
                        "type": "depository",
                        "mask": "4821",
                        "balances": {"iso_currency_code": "USD", "current": 12.5},
                    },
                    {
                        "account_id": "acc-2",
                        "name": "No Mask",
                        "type": "credit",
                        "balances": {},
                    },
                ]
            },
        )

    accounts = await _provider(handler, "access-x").get_accounts()
    assert accounts[0].mask == "4821"
    assert accounts[1].mask is None


async def test_connect_session_carries_redirect_uri(monkeypatch) -> None:
    """OAuth institutions need the registered redirect_uri — both modes."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(
        settings, "plaid_redirect_uri", "http://localhost:5173/connect/oauth-return"
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-x"})

    await _provider(handler).create_connect_session(client_user_id="user-1")
    await _provider(handler, "access-1").create_connect_session(client_user_id="user-1")
    assert seen[0]["redirect_uri"] == "http://localhost:5173/connect/oauth-return"
    assert seen[1]["redirect_uri"] == "http://localhost:5173/connect/oauth-return"


async def test_connect_session_omits_redirect_uri_when_unset(monkeypatch) -> None:
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_redirect_uri", "")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-x"})

    await _provider(handler).create_connect_session(client_user_id="user-1")
    assert "redirect_uri" not in seen


async def test_institution_resolved_in_two_steps() -> None:
    """/item/get names the institution id; /institutions/get_by_id names
    the institution — id and name answered together (M13 CP1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/item/get":
            return httpx.Response(200, json={"item": {"institution_id": "ins_1"}})
        assert request.url.path == "/institutions/get_by_id"
        body = json.loads(request.content)
        assert body["institution_id"] == "ins_1"
        return httpx.Response(200, json={"institution": {"name": "First Platypus Bank"}})

    institution = await _provider(handler, "access-x").get_institution()
    assert institution.provider_institution_id == "ins_1"
    assert institution.name == "First Platypus Bank"


async def test_institution_empty_for_institutionless_item() -> None:
    """Some Items carry no institution; degrade to empty without a second
    call."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/item/get"
        return httpx.Response(200, json={"item": {"institution_id": None}})

    institution = await _provider(handler, "access-x").get_institution()
    assert institution.provider_institution_id is None and institution.name is None


async def test_connect_sessions_carry_investments_consent_in_both_modes() -> None:
    """M10 CP2 (issue #75): additional_consented_products — consent
    everywhere, billed nowhere until an endpoint is called. Never the
    products array (hides banks), never the auto-billing arrays. Update
    mode carries it too: that's the retrofit path for existing Items."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-x"})

    await _provider(handler).create_connect_session(client_user_id="user-1")
    await _provider(handler, "access-existing").create_connect_session(client_user_id="user-1")

    creation, update = seen
    assert creation["additional_consented_products"] == ["investments"]
    assert creation["products"] == ["transactions"]  # investments never rides products
    assert update["additional_consented_products"] == ["investments"]
    assert "products" not in update  # update mode still requests nothing


# --- Investments (M10 CP0, issue #73; PRD #72) ----------------------------------


def _holdings_response() -> dict:
    return {
        "accounts": [],
        "holdings": [
            {
                "account_id": "acc-brokerage",
                "security_id": "sec-aapl",
                "quantity": 10.5,
                "institution_price": 190.1234,
                "institution_price_as_of": "2026-07-18",
                "institution_value": 1996.3,
                "cost_basis": 1500.55,
                "iso_currency_code": "USD",
            },
            {
                "account_id": "acc-brokerage",
                "security_id": "sec-cash",
                "quantity": 5000,
                "institution_price": None,
                "institution_price_as_of": None,
                "institution_value": 5000,
                "cost_basis": None,
                "iso_currency_code": "JPY",
            },
        ],
        "securities": [
            {
                "security_id": "sec-aapl",
                "name": "Apple Inc.",
                "ticker_symbol": "AAPL",
                "type": "equity",
                "is_cash_equivalent": False,
            },
            {
                "security_id": "sec-cash",
                "name": "Yen cash",
                "ticker_symbol": None,
                "type": "cash",
                "is_cash_equivalent": True,
            },
        ],
    }


async def test_holdings_convert_money_and_keep_price_precision() -> None:
    """Money lands as exponent-aware minor units (JPY included); the
    per-share price is a quote, not an Amount — sub-cent precision is real
    for fund NAVs and survives untouched."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json=_holdings_response())

    batch = await _provider(handler, "access-x").get_holdings()
    assert seen["path"] == "/investments/holdings/get"
    assert seen["access_token"] == "access-x"

    by_sid = {h.provider_security_id: h for h in batch.holdings}
    aapl = by_sid["sec-aapl"]
    assert aapl.provider_account_id == "acc-brokerage"
    assert aapl.quantity == 10.5
    assert aapl.institution_price == 190.1234
    assert aapl.institution_value_minor == 199630  # 1996.30 USD
    assert aapl.cost_basis_minor == 150055
    cash = by_sid["sec-cash"]
    assert cash.institution_value_minor == 5000  # JPY: exponent 0, never *100
    assert cash.cost_basis_minor is None and cash.institution_price is None

    securities = {s.provider_security_id: s for s in batch.securities}
    assert securities["sec-aapl"].ticker_symbol == "AAPL"
    assert securities["sec-aapl"].type == "equity"
    assert securities["sec-cash"].ticker_symbol is None
    assert securities["sec-cash"].is_cash_equivalent is True


async def test_holdings_use_the_investments_timeout() -> None:
    """A fresh Item's synchronous extraction can block for minutes — the
    30s default that suits banking calls would misfire NETWORK_ERROR here
    (PRD #72)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"accounts": [], "holdings": [], "securities": []})

    from pinch_backend.providers import INVESTMENTS_TIMEOUT

    await _provider(handler, "access-x").get_holdings()
    assert INVESTMENTS_TIMEOUT > 30
    assert seen["timeout"]["read"] == INVESTMENTS_TIMEOUT


async def test_activities_drain_offset_pages_and_flip_signs() -> None:
    """/investments/transactions/get is offset-paginated (no cursor
    endpoint exists — PRD #72); Plaid's positive-is-cash-out becomes
    Pinch's negative-is-money-out, uniformly: buys negative, dividends
    and sells positive."""
    from datetime import date as date_type

    calls: list[dict] = []

    def txn(tid: str, amount: float, ttype: str, subtype: str) -> dict:
        return {
            "investment_transaction_id": tid,
            "account_id": "acc-brokerage",
            "security_id": "sec-aapl",
            "date": "2026-07-10",
            "name": f"{ttype} {tid}",
            "quantity": 0.4,
            "amount": amount,
            "price": 190.0,
            "fees": 0.25,
            "type": ttype,
            "subtype": subtype,
            "iso_currency_code": "USD",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/investments/transactions/get"
        calls.append(body)
        transactions = {
            0: [txn("t-buy", 7.7, "buy", "buy")],
            1: [txn("t-div", -8.72, "cash", "dividend")],
        }[body["options"]["offset"]]
        return httpx.Response(
            200,
            json={
                "accounts": [],
                "investment_transactions": transactions,
                "securities": [
                    {
                        "security_id": "sec-aapl",
                        "name": "Apple Inc.",
                        "ticker_symbol": "AAPL",
                        "type": "equity",
                        "is_cash_equivalent": False,
                    }
                ],
                "total_investment_transactions": 2,
            },
        )

    batch = await _provider(handler, "access-x").get_investment_activities(
        start_date=date_type.fromisoformat("2024-07-10"),
        end_date=date_type.fromisoformat("2026-07-10"),
    )
    # Offset advances by rows received (Plaid: offset = rows to skip) —
    # robust even when an institution answers short pages.
    assert [c["options"]["offset"] for c in calls] == [0, 1]
    assert all(c["options"]["count"] == 500 for c in calls)
    assert all(c["start_date"] == "2024-07-10" and c["end_date"] == "2026-07-10" for c in calls)

    by_id = {a.provider_activity_id: a for a in batch.activities}
    assert set(by_id) == {"t-buy", "t-div"}
    buy = by_id["t-buy"]
    assert buy.amount_minor == -770  # buy: Plaid +7.70 → money out
    assert buy.type == "buy"
    assert buy.price == 190.0
    assert buy.fees_minor == 25
    assert buy.quantity == 0.4
    dividend = by_id["t-div"]
    assert dividend.amount_minor == 872  # dividend: Plaid -8.72 → money in
    assert dividend.subtype == "dividend"
    assert {s.provider_security_id for s in batch.securities} == {"sec-aapl"}


async def test_activities_tolerate_missing_security_and_use_timeout() -> None:
    from datetime import date as date_type

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "accounts": [],
                "investment_transactions": [
                    {
                        "investment_transaction_id": "t-fee",
                        "account_id": "acc-brokerage",
                        "security_id": None,
                        "date": "2026-07-10",
                        "name": "ACCOUNT FEE",
                        "quantity": 0,
                        "amount": 1.5,
                        "price": None,
                        "fees": None,
                        "type": "fee",
                        "subtype": "account fee",
                        "iso_currency_code": "USD",
                    }
                ],
                "securities": [],
                "total_investment_transactions": 1,
            },
        )

    from pinch_backend.providers import INVESTMENTS_TIMEOUT

    batch = await _provider(handler, "access-x").get_investment_activities(
        start_date=date_type.fromisoformat("2024-07-10"),
        end_date=date_type.fromisoformat("2026-07-10"),
    )
    assert seen["timeout"]["read"] == INVESTMENTS_TIMEOUT
    (fee,) = batch.activities
    assert fee.provider_security_id is None
    assert fee.amount_minor == -150  # a fee is money out
    assert fee.fees_minor is None


async def test_item_status_probe_selects_the_diagnostic_fields() -> None:
    """The plaid-item CLI's wire shape: a plain /item/get (production
    rejects include_status with UNKNOWN_FIELDS) plus a one-row
    /transactions/sync read for transactions_update_status — the field
    that separates 'never finished' from 'heuristic misread'."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path == "/item/get":
            assert "include_status" not in body
            return httpx.Response(
                200,
                json={
                    "item": {
                        "item_id": "item-1",
                        "institution_id": "ins_1",
                        "billed_products": ["transactions"],
                        "consented_products": ["transactions", "investments"],
                        "available_products": ["investments"],
                        "error": None,
                    }
                },
            )
        assert request.url.path == "/transactions/sync"
        assert body["count"] == 1
        assert "cursor" not in body  # read-only probe: never resumes, never persists
        return httpx.Response(
            200,
            json={
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "",
                "has_more": False,
                "transactions_update_status": "TRANSACTIONS_UPDATE_STATUS_NOT_READY",
            },
        )

    report = await _provider(handler, "access-x").get_item_status()
    assert calls == ["/item/get", "/transactions/sync"]
    assert report["consented_products"] == ["transactions", "investments"]
    assert report["transactions_update_status"] == "TRANSACTIONS_UPDATE_STATUS_NOT_READY"
    assert report["transactions_probe"]["next_cursor_empty"] is True
    assert report["error"] is None


async def test_holdings_error_surfaces_code_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_code": "PRODUCT_NOT_READY",
                "error_message": "the requested product is not yet ready",
            },
        )

    with pytest.raises(ProviderError) as excinfo:
        await _provider(handler, "access-x").get_holdings()
    assert excinfo.value.code == "PRODUCT_NOT_READY"


# --- The provider registry (M13 CP1, issue #88) ---------------------------------


async def test_registry_materializes_a_bound_plaid_provider(monkeypatch) -> None:
    """``get_provider`` is the one materialization point: the connection's
    provider selects the client, the secret binds it."""
    from pinch_backend import providers
    from pinch_backend.models import ConnectionProvider
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "cid")
    monkeypatch.setattr(settings, "plaid_secret", "sec")
    provider = providers.get_provider(ConnectionProvider.PLAID, secret="access-x")
    assert isinstance(provider, PlaidProvider)
    assert provider._token == "access-x"  # the binding is the point


async def test_registry_materializes_a_bound_mx_provider(monkeypatch) -> None:
    """MX's binding is guids, not a token (ADR 0009): the enrollment's
    user guid and the connection's member guid ride the same registry."""
    from pinch_backend import providers
    from pinch_backend.models import ConnectionProvider
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "mx_client_id", "cid")
    monkeypatch.setattr(settings, "mx_api_key", "key")
    provider = providers.get_provider(ConnectionProvider.MX, user_guid="USR-1", member_guid="MBR-2")
    assert isinstance(provider, MXProvider)
    assert provider._user == "USR-1" and provider._member == "MBR-2"


async def test_registry_rejects_foreign_binding_kwargs() -> None:
    """Each provider's factory signature IS its credential shape (ADR
    0009): handing Plaid a guid or MX a secret is a caller bug, answered
    with a loud TypeError — never silently ignored."""
    from pinch_backend import providers
    from pinch_backend.models import ConnectionProvider

    with pytest.raises(TypeError):
        providers.get_provider(ConnectionProvider.PLAID, user_guid="USR-1")
    with pytest.raises(TypeError):
        providers.get_provider(ConnectionProvider.MX, secret="access-x")


async def test_provider_configured_is_per_provider(monkeypatch) -> None:
    """Partial configuration is a valid state (PRD #86 story 16): Plaid
    configured says nothing about MX, and vice versa."""
    from pinch_backend import providers
    from pinch_backend.models import ConnectionProvider
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "plaid_client_id", "cid")
    monkeypatch.setattr(settings, "plaid_secret", "sec")
    assert providers.provider_configured(ConnectionProvider.PLAID) is True
    assert providers.provider_configured(ConnectionProvider.MX) is False

    monkeypatch.setattr(settings, "mx_client_id", "cid")
    monkeypatch.setattr(settings, "mx_api_key", "key")
    assert providers.provider_configured(ConnectionProvider.MX) is True

    monkeypatch.setattr(settings, "plaid_client_id", "")
    assert providers.provider_configured(ConnectionProvider.PLAID) is False
    assert providers.provider_configured(ConnectionProvider.MX) is True


# --- The owned MX client (M13 CP2, issue #89) ------------------------------------
# Wire shapes from the CP0 spike's live captures (docs/research/
# mx-platform-api.md, "CP0 spike findings") — integration-environment
# empirical, not documentation guesses.


def _mx(handler, user_guid: str | None = None, member_guid: str | None = None) -> MXProvider:
    return MXProvider(
        client_id="cid",
        api_key="key",
        environment="sandbox",
        user_guid=user_guid,
        member_guid=member_guid,
        transport=httpx.MockTransport(handler),
    )


async def test_mx_requests_carry_basic_auth_and_the_vnd_accept() -> None:
    """HTTP Basic client_id:api_key plus the vnd media type on every call
    (CP0 spike: the Accept alone suffices, no Accept-Version) — and the
    credentials ride the header, never the payload."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["accept"] = request.headers["Accept"]
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"user": {"guid": "USR-1"}})

    await _mx(handler).create_user(ledger_id="lid-1")
    assert seen["authorization"] == "Basic " + base64.b64encode(b"cid:key").decode()
    assert seen["accept"] == "application/vnd.mx.api.v1+json"
    assert "cid" not in seen["body"] and "key" not in seen["body"]


async def test_mx_create_user_posts_the_ledger_id() -> None:
    """The enrollment's container mint: POST /users with the pinch ledger
    id on MX's integrator id field — an operator's dashboard breadcrumb,
    never user data."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"user": {"guid": "USR-abc", "id": "pinch-lid-1"}})

    guid = await _mx(handler).create_user(ledger_id="lid-1")
    assert guid == "USR-abc"
    assert seen["path"] == "/users"
    assert seen["user"] == {"id": "pinch-lid-1"}


async def test_mx_connect_session_mints_a_widget_url() -> None:
    """The spike-verified request shape under the enrollment user; the
    answer is the hosted widget URL — the opaque string the frontend's
    widget consumes."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"widget_url": {"url": "https://int-widgets.moneydesktop.com/md/connect/xyz"}},
        )

    token = await _mx(handler, user_guid="USR-1").create_connect_session(client_user_id="user-1")
    assert token == "https://int-widgets.moneydesktop.com/md/connect/xyz"
    assert seen["path"] == "/users/USR-1/widget_urls"
    assert seen["widget_url"] == {
        "widget_type": "connect_widget",
        "mode": "aggregation",
        "ui_message_version": 4,
    }


async def test_mx_repair_session_pins_the_member() -> None:
    """A member-bound instance mints repair (MX's reconnect story):
    current_member_guid pins the widget to the broken login and the
    institution search disappears — never a fresh connect on top of a
    broken one."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"widget_url": {"url": "https://int-widgets.example/r"}})

    await _mx(handler, user_guid="USR-1", member_guid="MBR-9").create_connect_session(
        client_user_id="user-1"
    )
    assert seen["widget_url"]["current_member_guid"] == "MBR-9"
    assert seen["widget_url"]["disable_institution_search"] is True
    assert seen["widget_url"]["mode"] == "aggregation"


def _member_flow_handler(request: httpx.Request) -> httpx.Response:
    """The complete_connect wire flow: the verifying member read under
    OUR enrollment user, then the institution name in the same motion."""
    if request.url.path == "/users/USR-1/members/MBR-9":
        return httpx.Response(
            200,
            json={
                "member": {
                    "guid": "MBR-9",
                    "institution_code": "mxbank",
                    "connection_status": "CONNECTED",
                    "successfully_aggregated_at": "2026-08-04T19:59:17Z",
                }
            },
        )
    assert request.url.path == "/institutions/mxbank"
    return httpx.Response(200, json={"institution": {"code": "mxbank", "name": "MX Bank"}})


async def test_mx_complete_connect_verifies_under_our_enrollment() -> None:
    """The token is the widget's member guid, read back scoped to our
    enrollment user — the guid is never trusted naked. The answer is the
    connection's identity: member guid, institution code + name, and
    honestly no secret (ADR 0009)."""
    result = await _mx(_member_flow_handler, user_guid="USR-1").complete_connect("MBR-9")
    assert result.provider_item_id == "MBR-9"
    assert result.provider_institution_id == "mxbank"
    assert result.institution_name == "MX Bank"
    assert result.secret is None


async def test_mx_complete_connect_binds_the_verified_member() -> None:
    """The verified member binds the instance: the caller's next verbs
    (get_accounts) ride the same materialization — the PlaidProvider
    precedent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/USR-1/members/MBR-9/accounts":
            return httpx.Response(200, json={"accounts": [], "pagination": {"total_pages": 1}})
        return _member_flow_handler(request)

    provider = _mx(handler, user_guid="USR-1")
    await provider.complete_connect("MBR-9")
    assert await provider.get_accounts() == []


async def test_mx_foreign_member_guid_is_member_not_found() -> None:
    """A guid not under our enrollment answers 404 MX-side and surfaces
    as MEMBER_NOT_FOUND — the client's fault at the API surface (400),
    never upstream's."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Invalid route"}})

    with pytest.raises(ProviderError) as excinfo:
        await _mx(handler, user_guid="USR-1").complete_connect("MBR-stolen")
    assert excinfo.value.code == "MEMBER_NOT_FOUND"


async def test_mx_complete_connect_tolerates_institution_lookup_failure() -> None:
    """The display name is a nicety; the institution code rides the
    verifying member read and survives the name lookup failing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/USR-1/members/MBR-9":
            return _member_flow_handler(request)
        raise httpx.ConnectError("connection refused")

    result = await _mx(handler, user_guid="USR-1").complete_connect("MBR-9")
    assert result.provider_item_id == "MBR-9"
    assert result.provider_institution_id == "mxbank"
    assert result.institution_name is None


async def test_mx_accounts_are_member_scoped_with_kinds_and_signed_balances() -> None:
    """The load-bearing spike amendments in one shape: the listing is
    member-scoped (never user-scope — deletion ghosts), kinds map with
    the asset catch-all, decimal-dollar strings and numbers both convert
    exponent-aware, and debt-kind balances flip to Pinch's negative
    (CONTEXT.md: loans and credit carry negative balances; MX reports
    them positive). An overpaid card comes out positive."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "guid": "ACT-1",
                        "name": "Checking",
                        "type": "CHECKING",
                        "currency_code": "USD",
                        "balance": "1500.25",
                        "account_number": "XXXX4821",
                    },
                    {
                        "guid": "ACT-2",
                        "name": "Credit Card",
                        "type": "CREDIT_CARD",
                        "currency_code": "USD",
                        "balance": 100.5,
                    },
                    {
                        "guid": "ACT-3",
                        "name": "Home Loan",
                        "type": "MORTGAGE",
                        "currency_code": "USD",
                        "balance": 250000,
                    },
                    {
                        "guid": "ACT-4",
                        "name": "Overpaid Card",
                        "type": "CREDIT_CARD",
                        "currency_code": "USD",
                        "balance": -50,
                    },
                    {
                        "guid": "ACT-5",
                        "name": "Yen Savings",
                        "type": "SAVINGS",
                        "currency_code": "JPY",
                        "balance": 5000,
                    },
                    {
                        "guid": "ACT-6",
                        "name": "Beach House",
                        "type": "PROPERTY",
                        "currency_code": None,
                        "balance": None,
                    },
                    {
                        "guid": "ACT-7",
                        "name": "Brokerage",
                        "type": "INVESTMENT",
                        "currency_code": "USD",
                        "balance": "1996.30",
                    },
                ],
                "pagination": {"current_page": 1, "total_pages": 1},
            },
        )

    accounts = await _mx(handler, user_guid="USR-1", member_guid="MBR-9").get_accounts()
    assert seen["path"] == "/users/USR-1/members/MBR-9/accounts"

    by_guid = {a.provider_account_id: a for a in accounts}
    assert by_guid["ACT-1"].kind.value == "depository"
    assert by_guid["ACT-1"].balance_minor == 150025  # string decimal, no float round-trip
    assert by_guid["ACT-1"].mask == "4821"
    assert by_guid["ACT-2"].kind.value == "credit"
    assert by_guid["ACT-2"].balance_minor == -10050  # owed flips negative at the seam
    assert by_guid["ACT-2"].mask is None
    assert by_guid["ACT-3"].kind.value == "loan"
    assert by_guid["ACT-3"].balance_minor == -25_000_000
    assert by_guid["ACT-4"].balance_minor == 5000  # overpaid debt is honestly an asset
    assert by_guid["ACT-5"].balance_minor == 5000  # JPY: exponent 0, never *100
    assert by_guid["ACT-6"].kind.value == "asset"  # the catch-all
    assert by_guid["ACT-6"].balance_minor is None and by_guid["ACT-6"].currency is None
    assert by_guid["ACT-7"].kind.value == "investment"
    assert by_guid["ACT-7"].balance_minor == 199630


async def test_mx_accounts_drain_pagination() -> None:
    """MX paginates everything; the client drains total_pages so a
    many-account member never truncates."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        page = int(request.url.params["page"])
        account = {
            "guid": f"ACT-{page}",
            "name": f"Account {page}",
            "type": "CHECKING",
            "currency_code": "USD",
            "balance": 1,
        }
        return httpx.Response(
            200,
            json={"accounts": [account], "pagination": {"current_page": page, "total_pages": 2}},
        )

    accounts = await _mx(handler, user_guid="USR-1", member_guid="MBR-9").get_accounts()
    assert [a.provider_account_id for a in accounts] == ["ACT-1", "ACT-2"]
    assert [c["page"] for c in calls] == ["1", "2"]
    assert all(c["records_per_page"] == "100" for c in calls)


async def test_mx_remove_item_deletes_the_member() -> None:
    """DELETE the member, member-scoped; MX answers 204 with no body."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    await _mx(handler, user_guid="USR-1", member_guid="MBR-9").remove_item()
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/users/USR-1/members/MBR-9"


async def test_mx_remove_item_maps_gone_to_member_not_found() -> None:
    """The provider already not knowing the member is disconnect's
    success case — surfaced as MEMBER_NOT_FOUND for the API's
    ALREADY_DISCONNECTED tolerance, Plaid's ITEM_NOT_FOUND analog."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Invalid route"}})

    with pytest.raises(ProviderError) as excinfo:
        await _mx(handler, user_guid="USR-1", member_guid="MBR-9").remove_item()
    assert excinfo.value.code == "MEMBER_NOT_FOUND"


async def test_mx_error_surfaces_status_only_never_the_body() -> None:
    """MX error bodies are never parsed: free-form messages can carry
    anything, so the status code is the whole story — nothing from the
    payload may reach an error, a log, or a client."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "user SECRET-DETAIL did something odd"}}
        )

    with pytest.raises(ProviderError) as excinfo:
        await _mx(handler, user_guid="USR-1").create_connect_session(client_user_id="u")
    assert excinfo.value.code == "HTTP_400"
    assert "SECRET-DETAIL" not in str(excinfo.value)


async def test_mx_transport_fault_becomes_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ProviderError) as excinfo:
        await _mx(handler, user_guid="USR-1").create_user(ledger_id="lid-1")
    assert excinfo.value.code == "NETWORK_ERROR"


# --- MX transaction sync: watermark + window-diff (M13 CP3, issue #90) -----
# Wire shapes from the CP0 spike plus the CP3 live probe (docs/research/
# mx-platform-api.md): decimal amounts, account-relative CREDIT/DEBIT,
# day-granularity updated_at filters with mandatory inclusive bounds.


def _mx_txn(
    guid: str,
    amount: float | str,
    *,
    account_guid: str = "ACT-checking",
    type: str = "DEBIT",  # MX's own field name
    status: str = "POSTED",
    day: str = "2026-08-01",
    updated: str = "2026-08-04T21:57:11Z",
    description: str = "Ross",
    currency: str = "USD",
) -> dict:
    """One captured-shape MX transaction row (the fields the seam reads)."""
    return {
        "guid": guid,
        "account_guid": account_guid,
        "amount": amount,
        "type": type,
        "is_expense": type == "DEBIT",
        "status": status,
        "date": day,
        "updated_at": updated,
        "description": description,
        "currency_code": currency,
    }


class _MXSyncHandler:
    """Scripts the sync path's three reads — the member-status probe, the
    updated_at-filtered listing, and the date-filtered window re-list —
    and records every request for the read-only/param assertions."""

    def __init__(
        self,
        *,
        status: str = "CONNECTED",
        by_update: list[dict] | None = None,
        by_date: list[dict] | None = None,
    ) -> None:
        self.status = status
        self.by_update = by_update or []
        self.by_date = by_date or []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/users/USR-1/members/MBR-9/status":
            return httpx.Response(
                200,
                json={"member": {"connection_status": self.status, "is_being_aggregated": False}},
            )
        assert request.url.path == "/users/USR-1/members/MBR-9/transactions"
        params = dict(request.url.params)
        rows = self.by_update if "from_updated_at" in params else self.by_date
        return httpx.Response(
            200,
            json={
                "transactions": rows,
                "pagination": {"current_page": int(params["page"]), "total_pages": 1},
            },
        )


def _mx_sync(
    handler, stored: set[str] | None = None, calls: list[date] | None = None
) -> MXProvider:
    async def stored_window_ids(window_start: date) -> set[str]:
        if calls is not None:
            calls.append(window_start)
        return stored or set()

    return MXProvider(
        client_id="cid",
        api_key="key",
        environment="sandbox",
        user_guid="USR-1",
        member_guid="MBR-9",
        stored_window_ids=stored_window_ids,
        transport=httpx.MockTransport(handler),
    )


async def test_mx_initial_sync_is_one_wide_posted_only_listing() -> None:
    """No cursor is the initial backfill: one member-scoped date-window
    listing (explicit bounds — MX's filters ride in mandatory pairs and
    the endpoint's implicit default would truncate), PENDING filtered at
    the seam, everything landing in ``modified`` (the engine's upsert
    stance — dedupe by guid is what makes replay safe by construction),
    and the whole pass is GET-only: read-sync never touches MX's
    throttled aggregate endpoint (PRD #86: Refresh means re-read)."""
    today = datetime.now(UTC).date()
    handler = _MXSyncHandler(
        by_date=[
            _mx_txn("TRN-1", 72.33, description="Ross"),
            _mx_txn("TRN-2", "4.62", type="CREDIT", description="Transfer"),
            _mx_txn("TRN-3", 8.90, status="PENDING", description="Pending card hold"),
        ]
    )
    batch = await _mx_sync(handler).sync_transactions(None)

    listing_params = [
        dict(r.url.params) for r in handler.requests if r.url.path.endswith("/transactions")
    ]
    assert len(listing_params) == 1  # the backfill window already covers the re-list window
    assert (
        listing_params[0]["from_date"]
        == (today - timedelta(days=MX_INITIAL_WINDOW_DAYS)).isoformat()
    )
    assert (
        listing_params[0]["to_date"] == (today + timedelta(days=MX_SYNC_LOOKAHEAD_DAYS)).isoformat()
    )
    assert listing_params[0]["records_per_page"] == "1000"
    assert all(r.method == "GET" for r in handler.requests)
    assert not any("aggregate" in r.url.path for r in handler.requests)

    assert batch.added == []  # re-derivation can't tell add from modify: upsert stance
    assert [t.provider_transaction_id for t in batch.modified] == ["TRN-1", "TRN-2"]
    assert all(t.pending is False for t in batch.modified)  # posted-only v1
    assert batch.modified[0].amount_minor == -7233  # DEBIT: money out
    assert batch.modified[1].amount_minor == 462  # CREDIT: money in (string decimal)
    assert batch.modified[0].date == date.fromisoformat("2026-08-01")
    assert batch.modified[0].description == "Ross"
    assert batch.modified[0].provider_account_id == "ACT-checking"
    assert batch.modified[0].currency == "USD"


async def test_mx_credit_debit_signs_are_account_relative_on_every_segment() -> None:
    """The spike's amendment, empirically re-verified for CP3 across all
    MX Bank segments: CREDIT is money into the account and DEBIT money
    out, on debt segments too — a mortgage payment arrives CREDIT on the
    MORTGAGE account and lands positive (Pinch's transfer convention:
    money into the loan), a late fee DEBIT lands negative. The trap this
    test guards is borrowing the balance seam's debt-kind sign flip:
    balances flip, transactions never do. JPY proves the conversion stays
    exponent-aware."""
    handler = _MXSyncHandler(
        by_date=[
            _mx_txn(
                "TRN-pay", 1500.25, account_guid="ACT-mortgage", type="CREDIT",
                description="Mortgage Payment",
            ),
            _mx_txn(
                "TRN-fee", 17.85, account_guid="ACT-mortgage", type="DEBIT",
                description="Late Fee",
            ),
            _mx_txn(
                "TRN-cardpay", 20.71, account_guid="ACT-card", type="CREDIT",
                description="Credit Card Payment",
            ),
            _mx_txn("TRN-buy", 69.03, account_guid="ACT-card", type="DEBIT", description="Gap"),
            _mx_txn(
                "TRN-yen", 5000, account_guid="ACT-yen", type="DEBIT",
                description="Ramen", currency="JPY",
            ),
        ]
    )  # fmt: skip
    batch = await _mx_sync(handler).sync_transactions(None)
    amounts = {t.provider_transaction_id: t.amount_minor for t in batch.modified}
    assert amounts == {
        "TRN-pay": 150_025,  # CREDIT on the debt account: money in, positive
        "TRN-fee": -1785,  # DEBIT on the debt account: money out, negative
        "TRN-cardpay": 2071,
        "TRN-buy": -6903,
        "TRN-yen": -5000,  # exponent 0: never *100
    }


async def test_mx_incremental_sync_rides_the_inclusive_watermark_pair() -> None:
    """A persisted cursor is a date watermark (CP0 spike: the filters are
    day-granularity only — unix timestamps rejected — and both bounds are
    mandatory together, both inclusive). Inclusive means the watermark
    day replays every sync: the overlap row lands in ``modified`` again,
    priced in because ingestion dedupes by guid — replay-safe by
    construction. The cursor advances to the max updated_at date MX
    itself reported, never past it."""
    handler = _MXSyncHandler(
        by_update=[
            _mx_txn("TRN-overlap", 10.00, updated="2026-08-01T09:00:00Z"),
            _mx_txn("TRN-new", 20.00, updated="2026-08-03T12:00:00Z"),
        ]
    )
    batch = await _mx_sync(handler).sync_transactions("2026-08-01")

    today = datetime.now(UTC).date()
    listing_params = [
        dict(r.url.params) for r in handler.requests if r.url.path.endswith("/transactions")
    ]
    assert len(listing_params) == 2  # the watermark listing + the window re-list
    assert listing_params[0]["from_updated_at"] == "2026-08-01"
    assert (
        listing_params[0]["to_updated_at"]
        == (today + timedelta(days=MX_SYNC_LOOKAHEAD_DAYS)).isoformat()
    )
    assert (
        listing_params[1]["from_date"]
        == (today - timedelta(days=MX_REMOVAL_WINDOW_DAYS)).isoformat()
    )
    assert "from_updated_at" not in listing_params[1]

    assert {t.provider_transaction_id for t in batch.modified} == {"TRN-overlap", "TRN-new"}
    assert batch.next_cursor == "2026-08-03"  # MX's clock, not ours


async def test_mx_watermark_never_regresses_and_starts_a_day_early() -> None:
    """Monotonic against the old watermark (a quiet day must not rewind
    it), and a member with nothing to observe starts at yesterday — one
    day of overlap as provider-clock-skew insurance."""
    quiet = _MXSyncHandler(by_update=[])
    batch = await _mx_sync(quiet).sync_transactions("2026-08-01")
    assert batch.next_cursor == "2026-08-01"

    fresh = _MXSyncHandler(by_date=[])
    batch = await _mx_sync(fresh).sync_transactions(None)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    assert batch.next_cursor == yesterday.isoformat()
    assert batch.modified == [] and batch.removed == []


async def test_mx_removed_is_the_window_diff_against_stored_ids() -> None:
    """Deletions manifest only as absence (CP0 spike: no tombstone field
    exists anywhere in the shape), so ``removed`` is stored-minus-present
    over the rolling window — and doorbell payloads are never read (ADR
    0008/0009: MX's webhook-only deletion signal is rejected as a
    correctness dependency). Ids still listed stay; ids we hold that MX
    no longer lists inside the window retract."""
    calls: list[date] = []
    handler = _MXSyncHandler(
        by_update=[],
        by_date=[_mx_txn("TRN-keep", 5.00), _mx_txn("TRN-also", 6.00)],
    )
    provider = _mx_sync(handler, stored={"TRN-keep", "TRN-also", "TRN-gone"}, calls=calls)
    batch = await provider.sync_transactions("2026-08-01")

    assert batch.removed == ["TRN-gone"]
    assert calls == [datetime.now(UTC).date() - timedelta(days=MX_REMOVAL_WINDOW_DAYS)]


async def test_mx_sync_drains_listing_pagination() -> None:
    """A member with more rows than one page never truncates."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"member": {"connection_status": "CONNECTED"}})
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "transactions": [_mx_txn(f"TRN-{page}", 1.00, day="2026-08-02")],
                "pagination": {"current_page": page, "total_pages": 2},
            },
        )

    batch = await _mx_sync(handler).sync_transactions(None)
    assert [t.provider_transaction_id for t in batch.modified] == ["TRN-1", "TRN-2"]


@pytest.mark.parametrize("status", ["CHALLENGED", "DENIED", "LOCKED", "FAILED"])
async def test_mx_sync_raises_the_member_status_before_listing(status: str) -> None:
    """The status probe comes first: a dead or broken login raises with
    the member status as the code — the engine's one error contract maps
    reauth statuses to reauth_required and lets the FAILED family ride
    the transient ladder — and no data listing ever follows."""
    handler = _MXSyncHandler(status=status, by_date=[_mx_txn("TRN-1", 1.00)])
    with pytest.raises(ProviderError) as excinfo:
        await _mx_sync(handler).sync_transactions(None)
    assert excinfo.value.code == status
    assert not any(r.url.path.endswith("/transactions") for r in handler.requests)


@pytest.mark.parametrize("status", ["CONNECTED", "DELAYED", "DEGRADED"])
async def test_mx_sync_proceeds_on_benign_statuses(status: str) -> None:
    """Statuses outside both broken families proceed: the data MX holds
    is readable even mid-trouble, and a successful sync heals."""
    handler = _MXSyncHandler(status=status, by_date=[_mx_txn("TRN-1", 1.00)])
    batch = await _mx_sync(handler).sync_transactions(None)
    assert [t.provider_transaction_id for t in batch.modified] == ["TRN-1"]


async def test_mx_unreadable_cursor_self_heals_into_a_backfill() -> None:
    """A cursor that isn't a date (however it got there) is treated as
    the initial backfill — replay-safe by construction, so the remedy is
    free — instead of wedging the sync."""
    handler = _MXSyncHandler(by_date=[_mx_txn("TRN-1", 1.00)])
    batch = await _mx_sync(handler).sync_transactions("plaid-shaped-cursor-blob")
    listing_params = [
        dict(r.url.params) for r in handler.requests if r.url.path.endswith("/transactions")
    ]
    assert "from_date" in listing_params[0]
    assert [t.provider_transaction_id for t in batch.modified] == ["TRN-1"]
