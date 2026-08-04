"""Opt-in live smoke against MX's integration environment (M13 CP2,
issue #89).

Proves the owned MX client speaks actual int-api.mx.com: user creation,
widget-URL mint, a server-side MX Bank member (the widget's stand-in —
MX has no Plaid-style sandbox token, but the integration environment
lets members be created directly with the scripted mxbank credentials),
the verifying member read, member-scoped accounts with signed balances,
and member deletion. Everything created MX-side is deleted at the end,
user included — success or failure.

Never CI-gating — the module skips without credentials in the
environment. The conftest blanks ``PINCH_MX_*`` with setdefault, so a
shell export survives to here:

    PINCH_MX_CLIENT_ID=... PINCH_MX_API_KEY=... \
        uv run pytest tests/test_mx_sandbox_live.py

(Values living only in .env do NOT opt in: the suite stays hermetic.)
"""

import asyncio
import os
import uuid

import pytest

from pinch_backend.providers import MX_BASE_URLS, MXProvider, ProviderError

CLIENT_ID = os.environ.get("PINCH_MX_CLIENT_ID", "")
API_KEY = os.environ.get("PINCH_MX_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not (CLIENT_ID and API_KEY),
    reason="live MX integration smoke: set PINCH_MX_CLIENT_ID / PINCH_MX_API_KEY to run",
)


async def _mint_mx_bank_member(provider: MXProvider, user_guid: str) -> str:
    """The widget shortcut: create an MX Bank member server-side with the
    scripted credentials (mxuser + any non-scripted password connects —
    docs.mx.com/resources/test-platform/mxbank). Uses the client's
    private ``_request`` the way the Plaid smoke uses a raw httpx client
    for /sandbox/public_token/create — test-only plumbing, not product
    surface."""
    fields = await provider._request("GET", "/institutions/mxbank/credentials")
    credentials = []
    for c in fields["credentials"]:
        field_name = (c.get("field_name") or "").lower()
        if "password" in field_name:
            credentials.append({"guid": c["guid"], "value": "pinch-live-smoke"})
        else:
            credentials.append({"guid": c["guid"], "value": "mxuser"})
    data = await provider._request(
        "POST",
        f"/users/{user_guid}/members",
        json={"member": {"institution_code": "mxbank", "credentials": credentials}},
    )
    return data["member"]["guid"]


async def _wait_for_aggregation(provider: MXProvider, user_guid: str, member_guid: str) -> None:
    """The widget's memberConnected fires only after aggregation
    completes (~14s in the integration environment, CP0 spike); this
    poll plays that role for the server-side member."""
    for _attempt in range(24):
        data = await provider._request("GET", f"/users/{user_guid}/members/{member_guid}/status")
        status = data.get("member") or data
        if status.get("connection_status") == "CONNECTED" and status.get("has_processed_accounts"):
            return
        await asyncio.sleep(5)
    pytest.fail("MX Bank member never finished aggregating (~2 min)")


async def test_mx_connect_accounts_and_disconnect_against_integration() -> None:
    assert MX_BASE_URLS["sandbox"] == "https://int-api.mx.com"
    provider = MXProvider(client_id=CLIENT_ID, api_key=API_KEY, environment="sandbox")

    ledger_id = f"live-smoke-{uuid.uuid4().hex[:8]}"
    user_guid = await provider.create_user(ledger_id=ledger_id)
    assert user_guid.startswith("USR-")
    try:
        provider._user_guid = user_guid  # the enrollment binding the API layer would hand over

        session = await provider.create_connect_session(client_user_id="pinch-smoke")
        assert "int-widgets.moneydesktop.com" in session

        # A guid that exists nowhere must be MEMBER_NOT_FOUND — the
        # verify path against the real API.
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete_connect("MBR-does-not-exist")
        assert excinfo.value.code == "MEMBER_NOT_FOUND"

        member_guid = await _mint_mx_bank_member(provider, user_guid)
        await _wait_for_aggregation(provider, user_guid, member_guid)

        result = await provider.complete_connect(member_guid)
        assert result.provider_item_id == member_guid
        assert result.provider_institution_id == "mxbank"
        assert result.secret is None  # guid-shaped provider, honestly no secret

        accounts = await provider.get_accounts()
        assert accounts, "MX Bank member should carry accounts"
        assert all(a.provider_account_id.startswith("ACT-") for a in accounts)
        balances = [a for a in accounts if a.balance_minor is not None]
        assert balances, "aggregated accounts should report balances"
        for a in balances:
            if a.kind.value in ("credit", "loan"):
                # The seam's segment-aware sign: owed is negative.
                assert a.balance_minor <= 0

        await provider.remove_item()
        # Member-scope 404s immediately after deletion (CP0 spike).
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete_connect(member_guid)
        assert excinfo.value.code == "MEMBER_NOT_FOUND"
    finally:
        # The spike's hygiene: the throwaway user is hard-deleted,
        # members and all, pass or fail.
        await provider._request("DELETE", f"/users/{user_guid}")
