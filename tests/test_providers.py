"""The owned Plaid client against Plaid's wire shapes (M7 CP1, issue #33).

httpx.MockTransport scripts Plaid's documented JSON — the kind mapping,
currency extraction, backfill depth, and error surfacing are proven here
without a network; the opt-in live-sandbox smoke (CP2) proves the same
client against the real thing.
"""

import json

import httpx
import pytest

from pinch_backend.providers import BACKFILL_DAYS, PlaidProvider, ProviderError


def _provider(handler) -> PlaidProvider:
    return PlaidProvider(
        client_id="cid",
        secret="sec",
        environment="sandbox",
        transport=httpx.MockTransport(handler),
    )


async def test_link_token_carries_backfill_and_credentials() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"link_token": "link-sandbox-123"})

    token = await _provider(handler).create_link_token(client_user_id="user-1")
    assert token == "link-sandbox-123"
    assert seen["path"] == "/link/token/create"
    assert seen["client_id"] == "cid" and seen["secret"] == "sec"
    assert seen["user"] == {"client_user_id": "user-1"}
    assert seen["transactions"] == {"days_requested": BACKFILL_DAYS}
    assert BACKFILL_DAYS == 730  # PRD #31: two years of history requested


async def test_exchange_parses_token_and_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/item/public_token/exchange"
        return httpx.Response(200, json={"access_token": "access-sandbox-x", "item_id": "item-x"})

    exchanged = await _provider(handler).exchange_public_token("public-x")
    assert exchanged.access_token == "access-sandbox-x"
    assert exchanged.item_id == "item-x"


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

    accounts = await _provider(handler).get_accounts("access-x")
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

    await _provider(handler).remove_item("access-x")
    assert seen["path"] == "/item/remove"
    assert seen["access_token"] == "access-x"


async def test_update_mode_link_token_carries_access_token() -> None:
    """Reauth repair (PRD #31): the same endpoint in update mode — the
    Item's access token rides instead of a products request."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"link_token": "link-update-1"})

    token = await _provider(handler).create_link_token(
        client_user_id="user-1", access_token="access-broken"
    )
    assert token == "link-update-1"
    assert seen["access_token"] == "access-broken"
    assert "products" not in seen  # update mode repairs; it doesn't re-request


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

    accounts = await _provider(handler).get_accounts("access-x")
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

    batch = await _provider(handler).sync_transactions("access-x", cursor="cursor-start")
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

    batch = await _provider(handler).sync_transactions("access-x", cursor=None)
    assert batch.next_cursor == "c1"


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
        await _provider(handler).exchange_public_token("public-stale")
    assert excinfo.value.code == "INVALID_PUBLIC_TOKEN"
