"""Probe-then-decide reconciliation (M11 CP3, issue #81; ADR 0008;
provider-aware since M13 CP4, issue #91; ADR 0009).

The periodic safety net that isn't polling: webhooks are best-effort
(delivery retries stop after 24h — and MX registration is a dashboard
chore nobody can probe), so once a day per connection the reconciler
asks the provider — one free read — whether anything needs us. The
verdicts are per-provider, to each provider's honest extent. Plaid,
three: the Item's registered webhook isn't ours → re-register and sync
(the pre-M11 retrofit and the rotated-tunnel healer — the only
registration home besides link-token creation); Plaid's own
last-successful-update postdates our last sync → a doorbell went
missing, enqueue the normal sync and say so loudly; nothing new → stamp
``last_reconciled_at`` and do nothing. MX, two: the registration
verdict is honestly absent (dashboard-side, unprobeable — ADR 0009);
a member whose last successful aggregation postdates our last sync, or
whose MX-side status went broken without us hearing, is a missed
doorbell — sync, loudly; quiet stamps. Because MX self-aggregates
nightly, a dead or misregistered dashboard URL surfaces as daily
``webhook.missed`` warnings — the loudness IS the registration story.
A quiet account costs one free call per day and zero syncs —
reconciliation cost tracks failures, not users.

Broken connections (reauth_required, error) are never probed: they have
their own repair paths, prompt since the ITEM webhooks (CP2).
"""

from datetime import datetime, timedelta

from pinch_backend import providers
from pinch_backend.crypto import decrypt_secret
from pinch_backend.jobs import enqueue_sync_connection
from pinch_backend.models import (
    Connection,
    ConnectionProvider,
    ConnectionStatus,
    Enrollment,
    utcnow,
)
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)

RECONCILE_STALENESS = timedelta(hours=24)
"""The per-connection examination cadence — fixed, regardless of how
often the tick fires (the tick only decides how promptly a connection
crosses this line). Both a recent sync and a recent probe count as
examined: a connection that just synced has nothing for the probe to
discover."""


def _due(connection: Connection, cutoff: datetime) -> bool:
    examined = [
        stamp
        for stamp in (connection.last_synced_at, connection.last_reconciled_at)
        if stamp is not None
    ]
    return not examined or max(examined) < cutoff


def _probeable(connection: Connection) -> bool:
    """Whether this pass can honestly probe the connection: the provider
    must be configured (get_provider must never materialize without
    credentials), and Plaid's probe additionally needs the per-connection
    token a never-completed exchange doesn't have. Partial configuration
    is a valid state (PRD #86 story 16): an unconfigured provider's rows
    simply wait."""
    if connection.provider is ConnectionProvider.MX:
        return settings.mx_configured
    return settings.plaid_configured and connection.encrypted_secret is not None


async def reconcile_pass() -> int:
    """One pass over every due connection, both providers; returns how
    many were examined. The periodic task and the manual CLI trigger both
    run exactly this."""
    cutoff = utcnow() - RECONCILE_STALENESS
    active = await Connection.where(lambda c: c.status == ConnectionStatus.ACTIVE).all()
    probeable = [c for c in active if _probeable(c)]
    due = [c for c in probeable if _due(c, cutoff)]
    for connection in due:
        await reconcile_connection(connection)
    log.info("reconcile.pass_completed", examined=len(due), active=len(probeable))
    return len(due)


async def reconcile_connection(connection: Connection) -> str:
    """Probe one connection and decide; returns the verdict (asserted in
    tests, aggregated in logs). A failed probe stamps nothing — the next
    pass retries instead of buying the failure 24 quiet hours. The stamp
    saves before any job defers (the repo's defer-after-commit stance):
    the enqueued sync must never race a write it can't see."""
    if connection.provider is ConnectionProvider.MX:
        verdict = await _probe_mx(connection)
    else:
        verdict = await _probe_plaid(connection)
    if verdict in ("probe_failed", "reregister_failed"):
        return verdict

    connection.last_reconciled_at = utcnow()
    await connection.save()
    if verdict in ("reregistered", "webhook_missed"):
        await enqueue_sync_connection(connection.id)
    log.info("reconcile.verdict", connection_id=str(connection.id), verdict=verdict)
    return verdict


async def _probe_plaid(connection: Connection) -> str:
    """The Plaid probe: one free /item/get, three verdicts."""
    # The full Plaid surface, deliberately (M13 CP1): the drift healer
    # needs update_webhook, which the universal protocol doesn't owe.
    assert connection.encrypted_secret is not None  # the pass filtered on it
    provider = providers.get_plaid_provider(secret=decrypt_secret(connection.encrypted_secret))
    try:
        state = await provider.get_item_state()
    except providers.ProviderError as error:
        log.warning("reconcile.probe_failed", connection_id=str(connection.id), code=error.code)
        return "probe_failed"

    if state.webhook != settings.plaid_webhook_url:
        # The registration healer: '' is the pre-M11 retrofit, anything
        # else is a rotated tunnel or changed deploy domain. Re-register,
        # then sync — the Item may have been ringing a dead URL. A
        # Plaid-only verdict (M13 CP4): MX has no registration to read.
        try:
            await provider.update_webhook(settings.plaid_webhook_url)
        except providers.ProviderError as error:
            log.warning(
                "reconcile.reregister_failed",
                connection_id=str(connection.id),
                code=error.code,
            )
            return "reregister_failed"
        return "reregistered"
    if _plaid_updated_after_us(connection, state):
        # A doorbell went missing: recover the data quietly, surface the
        # delivery problem loudly — the safety net must not hide what it
        # catches (PRD #77 story 14).
        log.warning("webhook.missed", connection_id=str(connection.id))
        return "webhook_missed"
    return "quiet"


async def _probe_mx(connection: Connection) -> str:
    """The MX probe (M13 CP4, #91): one lean member-status read — status
    plus last-successful-aggregation. No registration verdict exists
    (dashboard-side, unprobeable — ADR 0009): a dead URL is caught by
    the missed-doorbell verdict instead, daily, because MX aggregates
    nightly whether or not anyone listens."""
    ledger_id = connection.ledger_id
    assert ledger_id is not None
    enrollment = await Enrollment.where(
        lambda e, lid=ledger_id: (e.ledger_id == lid) & (e.provider == ConnectionProvider.MX)
    ).first()
    if enrollment is None:
        # Corrupted state, not a provider problem: MX connects only ever
        # ride an ensured enrollment (the sync engine's stance) — loud,
        # unstamped, retried next pass rather than silently skipped.
        log.error("reconcile.mx_enrollment_missing", connection_id=str(connection.id))
        return "probe_failed"
    provider = providers.get_provider(
        ConnectionProvider.MX,
        user_guid=enrollment.provider_user_id,
        member_guid=connection.provider_item_id,
    )
    try:
        state = await provider.get_item_state()
    except providers.ProviderError as error:
        log.warning("reconcile.probe_failed", connection_id=str(connection.id), code=error.code)
        return "probe_failed"

    broken = state.member_status in providers.MX_REAUTH_STATUSES | providers.MX_BROKEN_STATUSES
    if broken or _mx_aggregated_after_us(connection, state):
        # Either the aggregation stamp moved without a ring, or MX-side
        # health broke and the Connection Status doorbell never reached
        # us. One remedy for both: the same sync a Refresh would run —
        # its own status probe reads MX and the engine's existing
        # transitions record what it finds (the no-new-states law; the
        # reconciler writes no health) — plus the loud warning the
        # safety net owes (PRD #77 story 14).
        log.warning(
            "webhook.missed",
            connection_id=str(connection.id),
            member_status=state.member_status,
        )
        return "webhook_missed"
    return "quiet"


def _plaid_updated_after_us(connection: Connection, state: providers.ItemState) -> bool:
    """Transactions only, deliberately — a stated boundary, not an
    oversight. Nothing on our side stamps an investments sync (the PRD
    pinned ``last_reconciled_at`` as M11's only schema change), and
    HOLDINGS extraction moves near-daily on price churn, so comparing
    ``investments_updated_at`` against ``last_synced_at`` would cry
    webhook.missed and re-sync every market day for every investment
    connection — a false alarm drowning story 14's real signal. The
    investments failure modes stay bounded without it: a dead URL is
    verdict (a)'s catch, and a sporadically dropped HOLDINGS ring is
    re-rung by the next market day's price churn."""
    if state.transactions_updated_at is None:
        return False
    if connection.last_synced_at is None:
        # Plaid has data and we never completed a sync — whatever ring
        # should have started it went missing.
        return True
    return state.transactions_updated_at > connection.last_synced_at


def _mx_aggregated_after_us(connection: Connection, state: providers.ItemState) -> bool:
    """MX's one every-product stamp: ``successfully_aggregated_at``
    (riding ``transactions_updated_at`` — ItemState's docstring). MX
    self-aggregates nightly, so a healthy member's stamp keeps advancing
    whether or not the doorbell reaches us: a stamp postdating our last
    sync means a ring went missing — or was never registered, which is
    how a dead dashboard URL surfaces as this verdict every single day
    (ADR 0009). No investments boundary to mind here: the stamp is the
    banking aggregation's, and MX chains no investments job."""
    if state.transactions_updated_at is None:
        return False
    if connection.last_synced_at is None:
        # MX has aggregated and we never completed a sync — whatever
        # ring should have started it went missing.
        return True
    return state.transactions_updated_at > connection.last_synced_at
