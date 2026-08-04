"""Opt-in live smoke against real Plaid sandbox (M7 CP2, issue #34).

Proves the owned httpx client speaks actual Plaid: link-token create,
sandbox public token, exchange, accounts, one cursor sync. Never
CI-gating — the module runs only when BOTH guards pass (M13 CP4, #91):

- Credentials exported from the shell (the conftest blanks
  ``PINCH_PLAID_*`` with setdefault, so .env values do NOT opt in — the
  suite stays hermetic):

      PINCH_PLAID_CLIENT_ID=... PINCH_PLAID_SECRET=... \\
          uv run pytest tests/test_plaid_sandbox_live.py

- ``plaid_environment == "sandbox"``, hard: since the production cutover
  a developer .env may hold PINCH_PLAID_ENVIRONMENT=production, and a
  live module must never run under a production-configured instance —
  export PINCH_PLAID_ENVIRONMENT=sandbox alongside the credentials if
  the .env says otherwise. (Belt and suspenders: every URL below is
  also pinned to the sandbox base, so production is unreachable from
  this module regardless.)
"""

import asyncio
import os

import httpx
import pytest

from pinch_backend.providers import PLAID_BASE_URLS, PlaidProvider, ProviderError
from pinch_backend.settings import settings

CLIENT_ID = os.environ.get("PINCH_PLAID_CLIENT_ID", "")
SECRET = os.environ.get("PINCH_PLAID_SECRET", "")

pytestmark = [
    pytest.mark.skipif(
        not (CLIENT_ID and SECRET),
        reason="live Plaid sandbox smoke: set PINCH_PLAID_CLIENT_ID / PINCH_PLAID_SECRET to run",
    ),
    pytest.mark.skipif(
        settings.plaid_environment != "sandbox",
        reason="live Plaid smoke is sandbox-only: PINCH_PLAID_ENVIRONMENT must be sandbox",
    ),
]


async def _sandbox_public_token(products: list[str] | None = None) -> str:
    """The widget shortcut Plaid sandbox provides — server-side only.

    For investments, sandbox only loads data when the product rides
    ``initial_products`` (the documented custom-user limitation) — which
    also sidesteps Link arrays entirely, so the production
    ``additional_consented_products`` path stays production-verified only
    (PRD #72)."""
    async with httpx.AsyncClient(base_url=PLAID_BASE_URLS["sandbox"], timeout=30) as client:
        response = await client.post(
            "/sandbox/public_token/create",
            json={
                "client_id": CLIENT_ID,
                "secret": SECRET,
                "institution_id": "ins_109508",  # First Platypus Bank
                "initial_products": products or ["transactions"],
            },
        )
        response.raise_for_status()
        return response.json()["public_token"]


async def test_connect_accounts_and_sync_against_sandbox() -> None:
    provider = PlaidProvider(client_id=CLIENT_ID, secret=SECRET, environment="sandbox")

    session_token = await provider.create_connect_session(client_user_id="pinch-smoke-test")
    assert session_token.startswith("link-sandbox-")

    result = await provider.complete_connect(await _sandbox_public_token())
    assert result.secret is not None
    assert result.secret.get_secret_value().startswith("access-sandbox-")
    assert result.provider_item_id
    # complete_connect binds the instance (M13 CP1): every verb below
    # rides the minted credential without re-handling it.

    accounts = await provider.get_accounts()
    assert accounts, "sandbox item should carry accounts"
    assert all(a.provider_account_id for a in accounts)

    # A fresh Item's initial pull is asynchronous on Plaid's side; the
    # client surfaces the not-ready state as PRODUCT_NOT_READY and the
    # worker's retry ladder waits it out — this loop plays that role here.
    for _attempt in range(24):
        try:
            batch = await provider.sync_transactions(cursor=None)
            break
        except ProviderError as error:
            if error.code != "PRODUCT_NOT_READY":
                raise
            await asyncio.sleep(5)
    else:
        pytest.fail("sandbox initial transaction pull never became ready (~2 min)")
    assert batch.next_cursor  # a drained cursor, ready to persist

    await provider.remove_item()


async def test_investments_holdings_and_activities_against_sandbox() -> None:
    """M10 CP1 smoke (issue #74): a sandbox Item minted with investments in
    initial_products yields holdings and a 24-month activity window through
    the owned client, tolerating the fresh-Item extraction delay."""
    from datetime import date, timedelta

    provider = PlaidProvider(client_id=CLIENT_ID, secret=SECRET, environment="sandbox")
    await provider.complete_connect(await _sandbox_public_token(products=["investments"]))

    for _attempt in range(24):
        try:
            batch = await provider.get_holdings()
            break
        except ProviderError as error:
            if error.code != "PRODUCT_NOT_READY":
                raise
            await asyncio.sleep(5)
    else:
        pytest.fail("sandbox investments extraction never became ready (~2 min)")
    assert batch.holdings, "sandbox investment item should carry holdings"
    assert {s.provider_security_id for s in batch.securities} >= {
        h.provider_security_id for h in batch.holdings
    }

    end = date.today()
    activities = await provider.get_investment_activities(
        start_date=end - timedelta(days=730), end_date=end
    )
    assert activities.activities, "sandbox investment item should carry activity"
    assert all(a.provider_activity_id for a in activities.activities)

    await provider.remove_item()
