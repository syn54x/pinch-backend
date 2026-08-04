"""Opt-in live smoke against MX's integration environment (M13 CP2,
issue #89).

Proves the owned MX client speaks actual int-api.mx.com: user creation,
widget-URL mint, a server-side MX Bank member (the widget's stand-in —
MX has no Plaid-style sandbox token, but the integration environment
lets members be created directly with the scripted mxbank credentials),
the verifying member read, member-scoped accounts with signed balances,
the CP3 transaction sync (watermark, window diff, posted-only, segment
signs — issue #90), the CP4 reconciler probe (member status +
last-successful-aggregation — issue #91), and member deletion.
Everything created MX-side is deleted at the end, user included —
success or failure.

The webhook path is deliberately absent: registration is dashboard-only
(the CP0 spike's empirical 404s), so end-to-end doorbell delivery stays
gated on the operator registering /webhooks/mx/{secret} in the MX
dashboard — the receiver itself is fully proven by direct POST in
test_mx_webhooks_api, and a registration that never happens surfaces as
the reconciler's daily webhook.missed (ADR 0009).

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
from datetime import date

import pytest

from pinch_backend.providers import MX_BASE_URLS, MXProvider, ProviderError

CLIENT_ID = os.environ.get("PINCH_MX_CLIENT_ID", "")
API_KEY = os.environ.get("PINCH_MX_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not (CLIENT_ID and API_KEY),
    reason="live MX integration smoke: set PINCH_MX_CLIENT_ID / PINCH_MX_API_KEY to run",
)


async def _mint_mx_bank_member(
    provider: MXProvider, user_guid: str, *, password: str = "pinch-live-smoke"
) -> str:
    """The widget shortcut: create an MX Bank member server-side with the
    scripted credentials (mxuser + any non-scripted password connects,
    while scripted passwords drive every other member status —
    docs.mx.com/resources/test-platform/mxbank). Uses the client's
    private ``_request`` the way the Plaid smoke uses a raw httpx client
    for /sandbox/public_token/create — test-only plumbing, not product
    surface."""
    fields = await provider._request("GET", "/institutions/mxbank/credentials")
    credentials = []
    for c in fields["credentials"]:
        field_name = (c.get("field_name") or "").lower()
        if "password" in field_name:
            credentials.append({"guid": c["guid"], "value": password})
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
        if (
            status.get("connection_status") == "CONNECTED"
            and status.get("has_processed_accounts")
            and status.get("has_processed_transactions")
        ):
            # Transactions too (CP3): gating on accounts alone let the
            # first sync race MX's still-writing transaction pull, so the
            # incremental leg saw "new" rows that were merely late.
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

        # --- The reconciler's probe (M13 CP4, issue #91) -----------------
        # Against the real status endpoint: a freshly aggregated member
        # answers CONNECTED with a usable aggregation stamp, and the
        # registration read is honestly absent (webhook '').
        state = await provider.get_item_state()
        assert state.member_status == "CONNECTED"
        assert state.transactions_updated_at is not None
        assert state.transactions_updated_at.tzinfo is not None  # comparable to utcnow()
        assert state.webhook == ""  # dashboard-side, unprobeable (ADR 0009)

        accounts = await provider.get_accounts()
        assert accounts, "MX Bank member should carry accounts"
        assert all(a.provider_account_id.startswith("ACT-") for a in accounts)
        balances = [a for a in accounts if a.balance_minor is not None]
        assert balances, "aggregated accounts should report balances"
        for a in balances:
            if a.kind.value in ("credit", "loan"):
                # The seam's segment-aware sign: owed is negative.
                assert a.balance_minor <= 0

        # --- The transactions leg (M13 CP3, issue #90) -------------------
        # Bind the removed-derivation's memory the way the sync engine
        # does — pretending Pinch holds one id MX doesn't, so the live
        # window diff must name exactly it as removed (test-only private
        # assignment, the ``_user_guid`` precedent above).
        async def stored_window_ids(window_start: date) -> set[str]:
            return {"TRN-pinch-ghost"}

        provider._stored_window_ids = stored_window_ids

        batch = await provider.sync_transactions(None)
        assert batch.added == []  # re-derivation upserts: modified is the only lane
        assert batch.modified, "MX Bank seeds ~90 days of transactions"
        assert all(t.pending is False for t in batch.modified)  # posted-only v1
        assert batch.removed == ["TRN-pinch-ghost"]  # stored-minus-present, live
        date.fromisoformat(batch.next_cursor)  # the cursor is a date watermark

        # Sign semantics against real MX Bank data. MX Bank's random seed
        # attaches descriptions loosely (a "Credit Card Payment" row can
        # be generated in either direction, run to run), so description-
        # level assertions don't hold live — the deterministic per-segment
        # sign cases are pinned in the wire tests from captured shapes.
        # What IS seed-stable: both directions flow on debt accounts
        # (payments in, charges out), and conversion preserves that
        # diversity instead of collapsing it with a segment flip.
        debt = {a.provider_account_id for a in accounts if a.kind.value in ("credit", "loan")}
        debt_rows = [t for t in batch.modified if t.provider_account_id in debt]
        assert debt_rows, "MX Bank seeds debt-account transactions"
        assert any(t.amount_minor > 0 for t in debt_rows)  # CREDIT: money into the account
        assert any(t.amount_minor < 0 for t in debt_rows)  # DEBIT: money out

        # Incremental from the watermark: the inclusive day-granularity
        # bounds re-fetch the watermark day (overlap is the norm) and
        # every guid answered was already seen — the dedupe-by-guid
        # replay story, against the real API.
        first_guids = {t.provider_transaction_id for t in batch.modified}
        again = await provider.sync_transactions(batch.next_cursor)
        assert {t.provider_transaction_id for t in again.modified} <= first_guids
        assert date.fromisoformat(again.next_cursor) >= date.fromisoformat(batch.next_cursor)

        await provider.remove_item()
        # Member-scope 404s immediately after deletion (CP0 spike).
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete_connect(member_guid)
        assert excinfo.value.code == "MEMBER_NOT_FOUND"

        # A scripted DENIED member (password "INVALID") — the
        # deterministic reauth story Plaid's sandbox never allowed: the
        # sync-path status probe raises the member status as the code,
        # before any listing, and the engine maps it to reauth_required.
        # Minted after the disconnect: MX allows one member per
        # institution per user (a second mxbank member 409s).
        denied_guid = await _mint_mx_bank_member(provider, user_guid, password="INVALID")
        for _attempt in range(24):
            data = await provider._request(
                "GET", f"/users/{user_guid}/members/{denied_guid}/status"
            )
            if (data.get("member") or data).get("connection_status") == "DENIED":
                break
            await asyncio.sleep(5)
        else:
            pytest.fail("scripted INVALID member never reached DENIED (~2 min)")
        denied = MXProvider(
            client_id=CLIENT_ID,
            api_key=API_KEY,
            environment="sandbox",
            user_guid=user_guid,
            member_guid=denied_guid,
        )
        with pytest.raises(ProviderError) as excinfo:
            await denied.sync_transactions(None)
        assert excinfo.value.code == "DENIED"
        # The reconciler's probe sees the same broken status without
        # raising (CP4): the missed-doorbell verdict reads it and lets
        # the enqueued sync's own probe do the recording.
        denied_state = await denied.get_item_state()
        assert denied_state.member_status == "DENIED"
    finally:
        # The spike's hygiene: the throwaway user is hard-deleted,
        # members and all, pass or fail.
        await provider._request("DELETE", f"/users/{user_guid}")
