# Plaid webhooks — fact sheet (researched 2026-08-03, plaid.com/docs)

Grounding for PRD M11. Sources: plaid.com/docs Markdown mirrors
(`<path>/index.html.md`); production-empirical findings marked.

## The two webhook mechanisms

Per-Item webhooks (TRANSACTIONS, HOLDINGS, INVESTMENTS_TRANSACTIONS,
ITEM) are configured solely via the `webhook` URL on `/link/token/create`
(or `/item/webhook/update`) and need **no Dashboard configuration or
product enablement** beyond the products themselves. The Dashboard
"Event selection" page configures endpoints for *server-side* products
that don't go through Link (Transfer, Wallet, Bank Income, Identity
Verification) — none used by Pinch. — plaid.com/docs/api/webhooks/

## TRANSACTIONS

`SYNC_UPDATES_AVAILABLE` fires after `/transactions/sync` is first
called on an Item (the initial sync call arms it), then on any change,
with `initial_update_complete` / `historical_update_complete` flags.
Recovery is always "call /transactions/sync with your cursor". Legacy
INITIAL/HISTORICAL/DEFAULT_UPDATE webhooks apply only to /transactions/get
integrations. — plaid.com/docs/transactions/webhooks/

## INVESTMENTS

`HOLDINGS: DEFAULT_UPDATE` fires when quantity **or price** of a holding
changes — i.e. near-daily on market days for any account holding
securities, independent of user activity. `INVESTMENTS_TRANSACTIONS:
DEFAULT_UPDATE` / `HISTORICAL_UPDATE` for activity. Not documented:
whether these fire for additional_consented_products Items (empirical
check = our Stash Item). — plaid.com/docs/api/products/investments/

## ITEM lifecycle

ERROR (error object, e.g. ITEM_LOGIN_REQUIRED), PENDING_EXPIRATION
(EU/UK only, 7 days), PENDING_DISCONNECT (US/CA, 7 days,
INSTITUTION_MIGRATION | INSTITUTION_TOKEN_EXPIRATION), LOGIN_REPAIRED
(self-healed without update-mode), USER_PERMISSION_REVOKED,
WEBHOOK_UPDATE_ACKNOWLEDGED (sent to the *new* URL). —
plaid.com/docs/api/items/

## URL lifecycle

Per-Item, never per-client. Set at `/link/token/create`; changed via
`/item/webhook/update` (webhook: null removes). —
plaid.com/docs/api/items/

## Verification

`Plaid-Verification` header carries a JWT: require alg=ES256, fetch JWK
by `kid` via `/webhook_verification_key/get`, verify signature, check
`iat` within ~5 min, constant-time-compare the `request_body_sha256`
claim against the raw body's SHA-256. Cache JWKs per kid; `expired_at`
marks refresh. — plaid.com/docs/api/webhooks/webhook-verification/

## Delivery contract

200 within 10 seconds required. Retries up to 24 hours, exponential
backoff from 30s (~4x per step), honors Retry-After up to 4h. Beyond
the window the webhook is dropped permanently — no replay API. Docs
explicitly instruct: design for duplicates and out-of-order delivery;
prefer a "dumb" receiver that just enqueues. —
plaid.com/docs/api/webhooks/

## /item/get status (PRODUCTION-EMPIRICAL, 2026-08-03)

A plain `/item/get` (no parameters — `include_status` is rejected with
UNKNOWN_FIELDS) returns a top-level `status` object:
`transactions.last_successful_update` / `last_failed_update`,
`investments.last_successful_update` / `last_failed_update`, and
`last_webhook`. `item.webhook` carries the registered URL (empty string
when none). These are the reconciler's probe fields.

## Sandbox

`/sandbox/item/fire_webhook` supports TRANSACTIONS
(DEFAULT_UPDATE, SYNC_UPDATES_AVAILABLE, RECURRING_TRANSACTIONS_UPDATE),
HOLDINGS (DEFAULT_UPDATE), INVESTMENTS_TRANSACTIONS (DEFAULT_UPDATE),
ITEM (NEW_ACCOUNTS_AVAILABLE, LOGIN_REPAIRED, PENDING_DISCONNECT,
USER_PERMISSION_REVOKED, USER_ACCOUNT_REVOKED). Fires to the Item's
registered URL — unreachable from CI, hence signature-level API-seam
tests instead of live webhook smoke. — plaid.com/docs/api/sandbox/

## Endpoint requirements

http(s) URL; valid SSL if https; 200 within 10s. Dashboard Logs page
lists all sent webhooks. — plaid.com/docs/account/activity/
