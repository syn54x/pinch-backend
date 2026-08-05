# Webhook-driven sync: doorbells required, polling retired

When Plaid is configured, a webhook URL is required (startup-validated) —
there is no webhook-less mode, no degraded polling fallback, and no
config flag to opt out; self-hosters and developers use a tunnel (ngrok)
to be reachable. Polling cost grows with users while webhook cost grows
with actual data changes, and a "both modes" build means every sync
behavior exists twice. Three laws shape the design. **Doorbell-only**
(CONTEXT.md: Webhook): the receiver reads which connection rang and which
kind of change, enqueues the same jobs a manual Refresh would, and
answers 200 — payload contents (counts, completion flags) are never read,
because Plaid documents duplicate and out-of-order delivery and our sync
is replay-safe by construction; a smarter payload-driven orchestrator was
rejected as coupling for nothing the cursor doesn't already know.
**Webhooks never invent state**: ITEM lifecycle events may only write the
statuses the sync engine already writes — they change when we learn,
never what states exist. **Probe-then-decide reconciliation**: the
periodic safety net (webhooks are best-effort; delivery retries stop
after 24h) does not sweep-sync — it asks `/item/get` (free) whether
Plaid's `status.*.last_successful_update` postdates our last sync and
whether `item.webhook` is still our URL, and only syncs on a genuinely
missed update (logged loudly) or re-registers on URL drift. A quiet
account costs one free call per day and zero syncs, so reconciliation
cost tracks failures, not users — the sweep-all-nightly alternative was
rejected as polling readmitted through the service door. Readiness
polling dies with this: PRODUCT_NOT_READY becomes a quiet wait for the
doorbell instead of a retry ladder ending in a parked error.

Amended by ADR 0009: the webhook laws are provider-universal; the
enforcement mechanisms (registration, verification, healing) are
per-provider, to each provider's honest extent.
