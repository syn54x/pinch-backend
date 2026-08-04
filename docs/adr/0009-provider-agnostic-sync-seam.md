# The sync seam is provider-agnostic; doorbells are payload-free everywhere

MX joins Plaid as the second sync provider (M13), with Finicity a named
future third — multi-aggregator is product strategy: providers succeed at
different institutions, and Pinch's users deserve the union of their
reach. That forced the seam to stop being Plaid-flavored, and two
decisions here are the load-bearing ones.

**One lean protocol, shaped by what every aggregator has.** The
`SyncProvider` protocol keeps only universal verbs; the connect pair is
renamed to the right altitude (`create_connect_session` → an opaque
string the provider's widget consumes; `complete_connect` → the
connection's provider identity), and Plaid-only verbs
(`get_webhook_verification_key`, `update_webhook`) demote to
`PlaidProvider` methods consumed by Plaid-specific plumbing — the
precedent `get_item_status` set. Credentials move into each provider's
constructor: Plaid holds a per-connection token (encrypted at rest), MX
and Finicity hold only instance credentials plus guids, so
`encrypted_secret` is honestly NULL for guid-shaped providers and every
credential gate says which providers it means. A per-(provider, ledger)
enrollment row covers the providers that require a user/customer
container; Plaid never writes one. Both known credential topologies fit
with no schema change — verified against MX and Finicity primary docs
(docs/research/).

**The doorbell-only law (ADR 0008) binds every provider — payloads are
never read, whichever provider rings.** MX tempts hardest: deleted
transactions appear in no list endpoint, only in webhook payloads, and
MX webhooks are unsigned. Reading them would make webhook delivery
load-bearing for correctness (a missed ring = a ghost transaction
forever) and would trust an unauthenticated payload to delete financial
records — rejected on both counts. Instead MX's `sync_transactions`
satisfies the batch contract by re-deriving truth: an updated-at
watermark (serialized into the connection's cursor field) for adds and
modifications, plus a rolling re-list window diffed against stored
provider ids to compute removals — deletions older than the window are
invisible, accepted because MX deletions are overwhelmingly pending
cleanup (hard-deleted at 14 days), well inside any sane window. A
provider that cannot support payload-free re-derivation does not get
integrated. Unsigned doorbells authenticate by a per-instance secret URL
segment (constant-time compare, undifferentiated 401); forgery is capped
at triggering one idempotent, lock-serialized sync. ADR 0008's
enforcement becomes per-provider: Plaid keeps startup-validated URL
registration, JWT verification, and reconciler healing; MX registration
is a dashboard-side operator chore the API can neither probe nor heal —
a dead URL surfaces as daily loud `webhook.missed` warnings instead,
because MX self-aggregates nightly whether or not anyone listens.
