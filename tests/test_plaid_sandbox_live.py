"""Opt-in live smoke against real Plaid sandbox (M7 CP2, issue #34).

Proves the owned httpx client speaks actual Plaid: link-token create,
sandbox public token, exchange, accounts, one cursor sync. Never
CI-gating — the whole module skips without credentials in the environment:

    PINCH_PLAID_CLIENT_ID=... PINCH_PLAID_SECRET=... uv run pytest tests/test_plaid_sandbox_live.py
"""

import asyncio
import os

import httpx
import pytest

from pinch_backend.providers import PLAID_BASE_URLS, PlaidProvider, ProviderError

CLIENT_ID = os.environ.get("PINCH_PLAID_CLIENT_ID", "")
SECRET = os.environ.get("PINCH_PLAID_SECRET", "")

pytestmark = pytest.mark.skipif(
    not (CLIENT_ID and SECRET),
    reason="live Plaid sandbox smoke: set PINCH_PLAID_CLIENT_ID / PINCH_PLAID_SECRET to run",
)


async def _sandbox_public_token() -> str:
    """The widget shortcut Plaid sandbox provides — server-side only."""
    async with httpx.AsyncClient(base_url=PLAID_BASE_URLS["sandbox"], timeout=30) as client:
        response = await client.post(
            "/sandbox/public_token/create",
            json={
                "client_id": CLIENT_ID,
                "secret": SECRET,
                "institution_id": "ins_109508",  # First Platypus Bank
                "initial_products": ["transactions"],
            },
        )
        response.raise_for_status()
        return response.json()["public_token"]


async def test_link_exchange_accounts_and_sync_against_sandbox() -> None:
    provider = PlaidProvider(client_id=CLIENT_ID, secret=SECRET, environment="sandbox")

    link_token = await provider.create_link_token(client_user_id="pinch-smoke-test")
    assert link_token.startswith("link-sandbox-")

    exchanged = await provider.exchange_public_token(await _sandbox_public_token())
    assert exchanged.access_token.startswith("access-sandbox-")
    assert exchanged.item_id

    accounts = await provider.get_accounts(exchanged.access_token)
    assert accounts, "sandbox item should carry accounts"
    assert all(a.provider_account_id for a in accounts)

    # A fresh Item's initial pull is asynchronous on Plaid's side; the
    # client surfaces the not-ready state as PRODUCT_NOT_READY and the
    # worker's retry ladder waits it out — this loop plays that role here.
    for _attempt in range(24):
        try:
            batch = await provider.sync_transactions(exchanged.access_token, cursor=None)
            break
        except ProviderError as error:
            if error.code != "PRODUCT_NOT_READY":
                raise
            await asyncio.sleep(5)
    else:
        pytest.fail("sandbox initial transaction pull never became ready (~2 min)")
    assert batch.next_cursor  # a drained cursor, ready to persist

    await provider.remove_item(exchanged.access_token)
