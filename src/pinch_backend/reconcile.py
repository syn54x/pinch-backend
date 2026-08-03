"""Probe-then-decide reconciliation (M11 CP3, issue #81; ADR 0008).

The periodic safety net that isn't polling: webhooks are best-effort
(delivery retries stop after 24h), so once a day per connection the
reconciler asks Plaid — one free /item/get — whether anything needs us.
Three verdicts: the Item's registered webhook isn't ours → re-register
and sync (the pre-M11 retrofit and the rotated-tunnel healer — the only
registration home besides link-token creation); Plaid's own
last-successful-update postdates our last sync → a doorbell went
missing, enqueue the normal sync and say so loudly; nothing new → stamp
``last_reconciled_at`` and do nothing. A quiet account costs one free
call per day and zero syncs — reconciliation cost tracks failures, not
users.

Broken connections (reauth_required, error) are never probed: they have
their own repair paths, prompt since the ITEM webhooks (CP2).
"""

from datetime import datetime, timedelta

from pinch_backend import providers
from pinch_backend.crypto import decrypt_secret
from pinch_backend.jobs import enqueue_sync_connection
from pinch_backend.models import Connection, ConnectionStatus, utcnow
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


async def reconcile_pass() -> int:
    """One pass over every due connection; returns how many were examined.
    The periodic task and the manual CLI trigger both run exactly this."""
    cutoff = utcnow() - RECONCILE_STALENESS
    active = await Connection.where(
        lambda c: (c.status == ConnectionStatus.ACTIVE) & (c.encrypted_secret != None)  # noqa: E711
    ).all()
    due = [c for c in active if _due(c, cutoff)]
    for connection in due:
        await reconcile_connection(connection)
    log.info("reconcile.pass_completed", examined=len(due), active=len(active))
    return len(due)


async def reconcile_connection(connection: Connection) -> str:
    """Probe one connection and decide; returns the verdict (asserted in
    tests, aggregated in logs). A failed probe stamps nothing — the next
    pass retries instead of buying the failure 24 quiet hours. The stamp
    saves before any job defers (the repo's defer-after-commit stance):
    the enqueued sync must never race a write it can't see."""
    provider = providers.get_provider()
    access_token = decrypt_secret(connection.encrypted_secret)  # ty: ignore[invalid-argument-type]
    try:
        state = await provider.get_item_state(access_token)
    except providers.ProviderError as error:
        log.warning("reconcile.probe_failed", connection_id=str(connection.id), code=error.code)
        return "probe_failed"

    if state.webhook != settings.plaid_webhook_url:
        # The registration healer: '' is the pre-M11 retrofit, anything
        # else is a rotated tunnel or changed deploy domain. Re-register,
        # then sync — the Item may have been ringing a dead URL.
        try:
            await provider.update_webhook(access_token, settings.plaid_webhook_url)
        except providers.ProviderError as error:
            log.warning(
                "reconcile.reregister_failed",
                connection_id=str(connection.id),
                code=error.code,
            )
            return "reregister_failed"
        verdict = "reregistered"
    elif _plaid_updated_after_us(connection, state):
        # A doorbell went missing: recover the data quietly, surface the
        # delivery problem loudly — the safety net must not hide what it
        # catches (PRD #77 story 14).
        log.warning("webhook.missed", connection_id=str(connection.id))
        verdict = "webhook_missed"
    else:
        verdict = "quiet"

    connection.last_reconciled_at = utcnow()
    await connection.save()
    if verdict in ("reregistered", "webhook_missed"):
        await enqueue_sync_connection(connection.id)
    log.info("reconcile.verdict", connection_id=str(connection.id), verdict=verdict)
    return verdict


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
