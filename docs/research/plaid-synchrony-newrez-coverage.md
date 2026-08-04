# Plaid coverage: Synchrony store cards & NewRez mortgage (researched 2026-08-04)

Why: three accounts fail to connect through Link in production — Lowe's
Advantage (Synchrony), Amazon Prime card, and a NewRez/Shellpoint
mortgage. Question: Plaid-side gap or integration-side bug? Sources:
plaid.com/docs Markdown mirrors, plaid.com institution pages, and
**production-empirical probes of `/institutions/search` +
`/institutions/get_by_id` with `include_status: true`** using Pinch's
own production credentials (marked PRODUCTION-EMPIRICAL). Forum
reports cited only as corroborating anecdata, labeled as such.

## TL;DR verdicts

| Institution | In Plaid? | Verdict |
| --- | --- | --- |
| Synchrony Bank (+ store cards) | Yes, `transactions` supported | **Plaid-side outage.** `item_logins` **DOWN since 2026-07-04**, success = 0, 98.6% institution-side errors. Synchrony is non-OAuth (screen-scraped) and is currently rejecting Plaid logins wholesale. Nothing to fix in Pinch. |
| Lowe's Advantage card | Yes — separate entry "Lowe's Credit Card - MyLowe's Rewards Credit Card" (ins_114104) | **Plaid-side outage.** `item_logins` DOWN, success = 0, 100% institution errors; `transactions_updates` refresh STOPPED. |
| Amazon Prime **Store Card** (Synchrony) | Yes — "Amazon.com Store Card" (ins_110743) | **Plaid-side outage.** `item_logins` DOWN, success = 0.01, 99% institution errors. |
| Amazon Prime **Visa** (Chase) | Yes — Chase (ins_56), OAuth | **Works.** item_logins success 98.5%. If the user has the Visa, connect via "Chase", not any Amazon-named entry. |
| NewRez / Shellpoint mortgage | Yes — "NewRez (formerly New Penn Financing)" (ins_126282); "Shellpoint" returns **zero** results | **Plaid-side degradation.** `item_logins` DEGRADED, success = 6.7%, **93% Plaid-side** errors. Coverage exists but is currently near-unusable. |

The pre-research hypothesis — "servicer-only institutions hidden from
Link by the products filter" — is **disproven** for all three: every
one of them lists `transactions` in its Plaid product array, so all are
searchable in Link with Pinch's `products=["transactions"]` token.
The failures are connection-health failures, not discoverability.

## PRODUCTION-EMPIRICAL: institution entries (2026-08-04)

`/institutions/search` (production, US) returns:

- `ins_116589` **Synchrony Bank** — oauth: false — products include
  `transactions`, `transactions_refresh`, `recurring_transactions`;
  **no `liabilities`**, no `investments`.
- Store cards are **separate institution entries**, not sub-brands of
  ins_116589: `ins_114104` Lowe's Credit Card - MyLowe's Rewards
  (transactions + **liabilities**), `ins_136277` MyLowe's Pro Rewards
  (transactions, no liabilities), `ins_110743` Amazon.com Store Card
  (transactions + liabilities), plus CareCredit (ins_119404), eBay
  (ins_125573), American Eagle (ins_122977), Synchrony Mastercard
  (ins_122147), Fleet Farm Visa (ins_131941). All oauth: false.
- `ins_126282` **NewRez (formerly New Penn Financing)** — oauth: false —
  products: assets, balance, signal, `transactions`,
  `transactions_refresh` (+ CRA products). **No `liabilities`** — the
  inversion of the hypothesis: NewRez is transactions-capable but
  liabilities-incapable in Plaid.
- Query "Shellpoint" → **empty**. Users must search "NewRez".
- Plaid's public page corroborates Synchrony coverage (Assets, Auth,
  Balance, Transactions; US only; no OAuth mention) —
  plaid.com/institutions/synchrony-bank/. No plaid.com/institutions
  page exists for NewRez or Shellpoint (404s), but the API entry is
  authoritative.

## PRODUCTION-EMPIRICAL: health status (2026-08-04)

`/institutions/get_by_id` with `options.include_status: true`
(`status` field is docs-deprecated but still populated; breakdown
percentages sum to 1 across success/error_plaid/error_institution —
plaid.com/docs/api/institutions/):

| Institution | item_logins | success | error split | since |
| --- | --- | --- | --- | --- |
| Synchrony Bank | DOWN | 0.000 | inst 0.986 / plaid 0.014 | 2026-07-04 |
| Lowe's (ins_114104) | DOWN | 0.000 | inst 1.00 | 2026-08-04 |
| Amazon Store Card | DOWN | 0.010 | inst 0.99 | 2026-08-04 (tx_updates DOWN since 2026-05-21) |
| NewRez | DEGRADED | 0.067 | **plaid 0.933** | 2026-08-03 |
| Chase (contrast) | DEGRADED | 0.985 | — | — |

Reading: Synchrony-family failures are overwhelmingly
`error_institution` — the institution's side refusing/failing logins,
consistent with Synchrony's long history of aggressive MFA and
aggregator blocking (non-OAuth scraping). Existing Synchrony Items also
stop refreshing: `transactions_updates`/`liabilities_updates` show
`refresh_interval: STOPPED` on the store cards. NewRez failures are
overwhelmingly `error_plaid` — Plaid's own connector is broken there.
Anecdata (corroborating only): Moneydance forum, 2026-08-03 — "Plaid
has reported error for at least 2 weeks" for Sam's Club/Synchrony,
recurring "over and over for a long time"
(infinitekind.tenderapp.com/discussions/online-banking/1253461).

In Link, these surface as INSTITUTION_NOT_RESPONDING / INSTITUTION_DOWN
/ INSTITUTION_NOT_AVAILABLE; all three bounce the user back to the
Institution Select pane with "try again later or pick another
institution" as Plaid's prescribed handling —
plaid.com/docs/errors/institution/.

## How Link decides which institutions are searchable

- The `products` array filters strictly: "Only institutions that
  support all products in this array will be available in Link."
  Multiple products intersect — an institution missing any one is
  hidden. — plaid.com/docs/link/initializing-products/
- `required_if_supported_products` and `optional_products` do **not**
  affect institution or account filtering (docs state this
  explicitly), and neither does `additional_consented_products`.
- Same rule server-side: `/institutions/search`'s `products` param
  filters to institutions supporting **all** listed products; an
  institution with no product overlap with the client's enabled
  products is filtered out entirely. —
  plaid.com/docs/api/institutions/

Pinch already threads this needle correctly (providers.py):
`products=["transactions"]` + `additional_consented_products=
["investments"]` — deliberately chosen in M10 CP2 because putting
`investments` in `products` would hide every non-investment
institution, including all five accounts above.

## What the integrator could change

Almost nothing — this is not an integration defect:

- **Don't add products to the token.** Adding `liabilities` or
  `investments` to `products` would *shrink* coverage (NewRez, Fleet
  Farm, and MyLowe's Pro all lack `liabilities`; nobody here has
  `investments`). `required_if_supported_products` is safe but changes
  nothing about discoverability, only billing/consent.
- **Route Amazon Prime Visa holders to Chase.** The Prime *Visa* is a
  Chase card (OAuth, healthy); only the Prime *Store Card* is
  Synchrony. Link search for "Amazon" surfaces the Synchrony store
  card, which is down — a user with the Visa should search "Chase".
- **Tell NewRez users to search "NewRez"**, not "Shellpoint" — the
  latter matches nothing.
- **Consider surfacing institution health.** Pinch could probe
  `/institutions/get_by_id include_status` (as this research did) to
  show "Synchrony is currently down on Plaid's side" instead of a
  generic connect failure — the INSTITUTION_* error codes from Link's
  onExit metadata carry the same signal for free.
- **Wait / escalate.** For persistent DOWN status Plaid's prescribed
  path is a support ticket with `institution_id` + Link session ID —
  plaid.com/docs/errors/institution/. Synchrony has been at zero
  login success for a full month (since 2026-07-04); a ticket is
  warranted but Synchrony-side blocking has historically been
  slow-rolling.
