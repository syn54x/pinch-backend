# Finicity / Mastercard Open Finance — primary-source due diligence (researched 2026-08-04)

Why: evaluating Finicity (acquired by Mastercard in 2020; docs now live
under developer.mastercard.com) as a **third** `SyncProvider` alongside
Plaid and MX (see [docs/research/mx-platform-api.md](./mx-platform-api.md)
and [docs/research/plaid-synchrony-newrez-coverage.md](./plaid-synchrony-newrez-coverage.md)).
Same question as the MX pass: does Finicity fit Pinch's provider-agnostic
schema — per-(provider, ledger) customer/user id + a per-connection
`provider_item_id` + a nullable `encrypted_secret` populated only by
providers that hand back a bearer secret — with **zero schema changes**?

## Sources

Primary sources only, fetched directly:

- **github.com/Mastercard/open-banking-us-openapi** (`openbanking-us.yaml`,
  fetched raw, ~53.5k lines) — Mastercard's own published OpenAPI 3.0 spec
  for Open Finance US, along with its `README.md` and `bin/setup.sh`. This
  was the single richest source: every endpoint path, operationId, schema
  field, and parameter description quoted below is taken directly from
  this spec unless a doc page is cited instead.
- **docs.finicity.com** — confirmed dead as an independent site. Fetching
  it 302-redirects straight to
  `developer.mastercard.com/open-finance-us/documentation/`.
- **developer.mastercard.com/open-finance-us/documentation/…** — the
  current canonical prose docs, reached via a headless browser (the site
  is a client-rendered SPA; a plain fetch only returns an empty
  "Mastercard Developers" shell with a "Loading… Please wait" placeholder
  — noted here because it changes the verification method from the MX
  pass, not because anything was blocked). Pages actually rendered and
  read: `quick-start-guide/`, `onboarding/` (API Basics/Authentication),
  `webhooks/`, `webhooks/webhooks-connect/`,
  `products/manage/tx-push/`, `products/manage/data-access-tiers/`,
  `financial-institution/`, `financial-institution/supported-institutions/`
  (including live searches against its institution table — see §"Coverage"),
  `glossary/`, `products/manage/transaction-data/understanding-transaction-data/`,
  and `errors/request/max-limit-exceeded/`. Also note: **both**
  `open-banking-us` and `open-finance-us` URL prefixes are live; navigating
  to `open-banking-us/documentation/` silently resolves to
  `open-finance-us/documentation/` in the rendered page — Mastercard
  appears to be mid-rebrand from "Open Banking" to "Open Finance" and both
  names are in concurrent use as of this research.
- **developer.mastercard.com/open-finance-us/documentation/llms-full.txt**
  — the site's own LLM-oriented summary page, used only for two figures
  not found on any prose page (the 500-test-customer sandbox cap, a
  restated getting-started outline) — flagged individually where relied
  on, since it's a generated summary rather than a authored doc page.
- No Finicity/Mastercard developer credentials exist for this project —
  like the sibling MX file, this is **documentation-only due diligence**.
  There is no PRODUCTION-EMPIRICAL section. Third-party trackers
  (`openbankingtracker.com`, `supergood.ai`, G2 reviews) turned up in
  search results but were **not** cited as evidence for any API-behavior
  claim.

## TL;DR

| # | Question | Answer | Cite |
| - | -------- | ------ | ---- |
| 1 | Auth model | Partner ID + Partner Secret → `POST /aggregation/v2/partners/authentication` (header `Finicity-App-Key`) returns a `Finicity-App-Token`, valid 2h, refresh recommended at 90min. Every subsequent call needs **both** headers together (static App Key + rotating App Token) — no per-connection or per-customer secret anywhere in the spec. Not moved to OAuth2 client-credentials; still this bespoke scheme. | github.com/Mastercard/open-banking-us-openapi `openbanking-us.yaml` L106-171, developer.mastercard.com/open-finance-us/documentation/onboarding/ |
| 2 | Customer model | Explicit "testing" vs "active"(billable) create-customer split: `POST /aggregation/v2/customers/testing` / `.../customers/active`. `Customer` schema = `{id, username, firstName, lastName, phone, email, type, createdDate, lastModifiedDate}` — no secret field. Sandbox caps at 500 test customers per Partner ID. | `openbanking-us.yaml` L401-458, L40938-40963, developer.mastercard.com/open-finance-us/documentation/llms-full.txt |
| 3 | Connect flow | `POST /connect/v2/generate {partnerId, customerId}` → `{"link": "https://connect2.finicity.com?...&signature=...&ttl=..."}`, single-use, signed, no separate token exchange. Backend discovers accounts/`institutionLoginId` afterward via `GET /aggregation/v1/customers/{customerId}/accounts`. Optional `webhook` param streams signed session events server-side; SDK events available client-side as an alternative. | `openbanking-us.yaml` L172-210, L40689-40729, developer.mastercard.com/open-finance-us/documentation/quick-start-guide/ |
| 4 | Connection identity | `institutionLoginId` groups accounts by bank login — `int64` on the `Account` record (`NumericInstitutionLoginId`), `string` in URL path params (`InstitutionLoginId` schema). **No first-class "member"/"Institution Login" resource** — it's purely a grouping field + path segment. Generally stable but not permanently fixed: `MigrateInstitutionLoginAccounts` exists for legacy→OAuth technology migrations that can reassign it. | `openbanking-us.yaml` L41043-41045, L43553-43558, L44899-44905, L1651-1663 |
| 5 | Transaction sync | Pure `fromDate`/`toDate` (epoch seconds, both required) + offset paging, max 1000/page — **no cursor**. Tri-state `status`: `active`/`pending`/`shadow` (shadow = previously reported, now gone — treat as deleted). Standard history ≈180 days from account-add date; deeper backfill is a separate billable `POST .../transactions/historic` (up to 24 months, billed once per customer). | `openbanking-us.yaml` L2440-2465, L1115-1145, developer.mastercard.com/.../understanding-transaction-data/ |
| 6 | Webhooks | Three parallel families with **different signing schemes**: Data Connect webhooks (HMAC-SHA256 of body, key = Partner Secret, header `X-Finicity-Signature`); TxPUSH (HMAC-SHA256 of a constructed signing string, key = a per-subscription `signingKey` issued at subscribe-time, header `txpush-signature`); and a newer unified **OBWMS** (Open Banking Webhook Management System) that several legacy webhook-management endpoints are themselves marked `deprecated` in favor of ("to be replaced... during 2026") — an in-flight migration, flagged below. Retries: non-200 responses retried for 3 days with exponential backoff. | developer.mastercard.com/.../webhooks/webhooks-connect/, developer.mastercard.com/.../products/manage/tx-push/, `openbanking-us.yaml` L4042-4067 (deprecated: true) |
| 7 | Aggregation model | Accounts auto-refresh ~daily; manual `POST .../accounts` refresh exists but "client apps are not permitted to automate calls to the Refresh services." No published numeric rate limit; mechanism confirmed (App-Key-scoped "too many requests," plus a per-institution throttle, error `977`) but no number disclosed publicly. | `openbanking-us.yaml` L727-747, developer.mastercard.com/.../onboarding/#authentication, developer.mastercard.com/.../errors/request/max-limit-exceeded/ |
| 8 | Investments | No separate Investments product. Holdings are a `position` array **embedded directly on the Account object** (`CustomerAccountPosition`, via the standard Accounts endpoints). Investment transactions are **not** a separate endpoint either — they flow through the normal Transactions endpoints, flagged only by an `investmentTransactionType` field (e.g. `dividend`). | `openbanking-us.yaml` L41043-41056, L17400-17447 |
| 9 | Amount/sign | Sign convention **flips by account segment**: deposit accounts (checking/savings) use traditional positive=inflow/negative=outflow; investment **and** credit-card/line-of-credit accounts invert this (negative=inflow, positive=outflow). A `type` (`debit`/`credit`) field is sometimes present as a secondary signal. | developer.mastercard.com/.../understanding-transaction-data/ |
| 10 | Dev/sandbox access | Fully self-serve, free, unlimited **Test Drive** plan against mock "FinBank" institutions (no contract needed). Real institution data requires "Moving to Production" — either a free 30-day **Test Drive Premium** trial or a billable Production plan, and **both require an executed contract**, worked through a Sales Rep/CSM. US (+ UK/CA/AU-origin-IP) only for this product line. | developer.mastercard.com/.../onboarding/#environments, developer.mastercard.com/.../onboarding/#moving-to-production |
| 11 | **Zero-schema-change fit** | **Yes.** `customerId` maps onto the per-(provider,ledger) enrollment id; `institutionLoginId` maps onto `Connection.provider_item_id`; Finicity never hands back a per-connection bearer secret anywhere in the spec, so `Connection.encrypted_secret` stays `NULL` exactly as it does for MX. No third shape found. One non-blocking nuance: `institutionLoginId` is reassignable under Finicity-initiated legacy→OAuth migrations, a laxer stability guarantee than Plaid's `item_id` or MX's `member_guid`. | `openbanking-us.yaml` (Customer, Account schemas; no secret field anywhere), L1651-1663 (MigrateInstitutionLoginAccounts) |

## 1. Auth model

`POST /aggregation/v2/partners/authentication` — send `partnerId` +
`partnerSecret` in the JSON body plus the static `Finicity-App-Key`
header; response is `{"token": "..."}`. "The token is valid for two
hours and is required on all calls to the Finicity APIs... generate a
new one" when older than 90 minutes — `openbanking-us.yaml` L106-131
(`operationId: CreateToken`). This is corroborated verbatim on
developer.mastercard.com/open-finance-us/documentation/onboarding/#authentication
and the Quick Start Guide, both of which show the identical curl example
and a sample token `YBh22Sb9Es6e66Q7lWdt`.

Every other authenticated endpoint declares
`security: [{FinicityAppKey: [], FinicityAppToken: []}]` as **one**
requirement object (an AND, not an OR) — `openbanking-us.yaml`
L5101-5110 (`securitySchemes`). Concretely: `Finicity-App-Key` is a
static per-partner API key issued at project creation ("from the
developer dashboard"); `Finicity-App-Token` is the short-lived value
returned by the auth call above. Neither is per-customer or
per-connection — the same App-Key/App-Token pair is reused for every
customer and every connection under that partner.

Partner Secret itself does not expire but is rotatable via
`PUT /aggregation/v2/partners/authentication` (`ModifyPartnerSecret`,
`openbanking-us.yaml` L145-171); Mastercard recommends rotating it "at
least once every 12 months" — onboarding page. Five consecutive failed
`CreateToken` calls with a wrong secret locks the Partner ID (error
`24302` on all subsequent calls) until a support ticket unlocks it — same
page, corroborated by the identical "after five failed attempts... your
account will be locked" line inside the `CreateToken` description in the
OpenAPI spec itself (L118-121).

**Has this moved to OAuth2 client-credentials since the acquisition?**
No — this remains Mastercard's own Partner-ID/Secret → App-Token scheme,
unchanged in shape from Finicity's pre-acquisition docs as far as this
spec shows. One inconsistency worth flagging: a WebSearch-indexed
snippet of Mastercard's own service catalog (`developer.mastercard.com/llms.txt`)
tags the `open-finance` service as `Auth Type: OAuth1.0a` — that does not
match anything in the OpenAPI spec or the onboarding prose (no OAuth1
signature/nonce/timestamp mechanics appear anywhere for the
partner-authentication leg); flagged as an unresolved discrepancy in
Mastercard's own catalog metadata rather than a verified fact either way.

## 2. Customer model

Two creation endpoints, matching the "testing vs active" split cleanly
onto a per-(provider, ledger) enrollment:

- `POST /aggregation/v2/customers/testing` — `AddTestingCustomer`:
  "Enroll a testing customer (Test Drive accounts)... can access FinBank
  profiles... cannot access live financial institutions" —
  `openbanking-us.yaml` L401-422.
- `POST /aggregation/v2/customers/active` — `AddCustomer`: "Enroll an
  active customer, which is the actual owner of one or more real-world
  accounts. This is a billable customer" — L432-453.

Both return the same `Customer` shape (`openbanking-us.yaml` L40938-40963):
`{id, username, firstName, lastName, phone, email, type, createdDate,
lastModifiedDate}` — required fields are only `id`, `username`, `type`,
`createdDate`. **No secret or token field anywhere on this object.** The
returned `id` (e.g. `"1005061234"` in the Quick Start Guide's sample
response) is the durable identifier the integrator threads through every
subsequent call — the direct analogue of MX's `user_guid`.

Constraint found: the Sandbox/Test-Drive environment caps out at **500
test customers per Partner ID** (developer.mastercard.com/open-finance-us/documentation/llms-full.txt).
No explicit "one active customer per end-user, enforced how" statement
was found in the fetched pages beyond `username` presumably needing to be
unique per partner (not stated explicitly) — flagged below.

## 3. Connect flow

`POST /connect/v2/generate` requires `partnerId` + `customerId`
(`ConnectParameters` schema, `openbanking-us.yaml` L40689-40724); optional
fields include `redirectUri`, `webhook`, `webhookData`, `webhookHeaders`,
`experience`, `institutionSettings`, `fromDate`, `singleUseUrl`,
`isWebView`. Response is `ConnectUrl {link}` — a single field, no
account/member identifiers returned inline. The Quick Start Guide's
worked example:

```
POST https://api.finicity.com/connect/v2/generate
{ "partnerId": "{{partnerId}}", "customerId": "{{customerId}}" }

→ { "link": "https://connect2.finicity.com?customerId=1005061234&origin=url&partnerId=2423653942467&signature=91f44ab9...&timestamp=1651326873996&ttl=1651334073996" }
```

(developer.mastercard.com/open-finance-us/documentation/quick-start-guide/#step-3---generate-mastercard-data-connect-url).
The link itself is signed and time-boxed (`signature`, `ttl`) — no
separate token-exchange call exists, matching MX's shape (hand back a
URL, not a `public_token`) rather than Plaid's (`public_token` →
`access_token` exchange).

**What Connect hands back on success**: two independent mechanisms, both
optional and neither required to complete the flow:

- **Server-to-server webhook** — pass a `webhook` URL in the generate-URL
  request; Mastercard POSTs signed session-progress events to it
  (`eventType`, `payload`, wrapped with `customerId`/`consumerId`/`eventId`)
  as the customer moves through the flow — signed via
  `X-Finicity-Signature` = HMAC-SHA256(raw body, key = Partner Secret) —
  developer.mastercard.com/open-finance-us/documentation/webhooks/webhooks-connect/.
- **Client-side SDK events** — if using the Web/Mobile Data Connect SDK,
  an in-page event listener receives the same kind of progress
  notifications directly (Web SDK Events / User Events / Route Events) —
  developer.mastercard.com/open-finance-us/documentation/glossary/#data-connect-events.

Neither payload is required to proceed: the documented, guaranteed way to
learn what got connected is to call
`GET /aggregation/v1/customers/{customerId}/accounts` afterward and read
each returned account's `institutionLoginId` — exactly the pattern shown
in Step 5 of the Quick Start Guide ("Refresh Customer Accounts" then
inspect the account list).

## 4. Connection identity

`institutionLoginId` is the grouping key for "all accounts reachable by
one set of credentials at one institution" — directly analogous to MX's
`member_guid`. It shows up in two forms in the OpenAPI spec:

- As an **integer** field on every `Account` record: `institutionLoginId:
  $ref: '#/components/schemas/NumericInstitutionLoginId'`, where that
  schema is `{type: integer, format: int64, example: 1007302745}` —
  `openbanking-us.yaml` L41043-41045, L44899-44905.
- As a **string** path parameter used across account-scoped endpoints
  (`GET/POST/DELETE .../institutionLogins/{institutionLoginId}/accounts`,
  `.../institutionLogins/{institutionLoginId}`): `InstitutionLoginId:
  {type: string, example: '1007302745'}` — L43553-43558.

There is **no separate first-class resource** for it — no
`GET /institutionLogins/{id}` returning institution-login-level metadata
the way MX's `GET /users/{id}/members/{member_guid}` does. It exists
purely as (a) an attribute every `Account` carries and (b) a path segment
for the handful of institution-login-scoped operations: get accounts by
login (`GetCustomerAccountsByInstitutionLogin`), refresh by login
(`RefreshCustomerAccountsByInstitutionLogin`, and a `V2` variant for Data
Access Tiers customers), and revoke/delete by login
(`DeleteCustomerAccountsByInstitutionLogin`) —
`openbanking-us.yaml` L700-825.

Stability: generally durable, but **not guaranteed permanent**. A
dedicated endpoint, `GET /aggregation/v1/customers/{customerId}/institutionLogins/{institutionLoginId}/migrate`
(`operationId: MigrateInstitutionLoginAccounts`), exists specifically
because "the `institutionLoginId` parameter uses Finicity's internal FI
mapping to move accounts from the current FI legacy connection to the new
OAuth FI connection" — L1640-1663. In other words, when Finicity migrates
an institution's underlying connection technology (screen-scrape →
OAuth), the accounts under a customer can be reassigned to a **new**
`institutionLoginId`, and this endpoint is how an integrator would
reconcile that. Neither the Plaid nor the MX research files found an
analogous documented "your stable connection id may get swapped out from
under you by the provider" mechanism — this is a real, if edge-case,
operational difference worth flagging (see §11 and "What I could not
verify").

## 5. Transaction sync semantics

Two read endpoints, both pure date-range + offset paging, no cursor:

- `GET /aggregation/v3/customers/{customerId}/transactions` — all
  accounts for a customer, params `fromDate`/`toDate` (both **required**,
  epoch seconds), `start`, `limit`, `sort`, `includePending` —
  `openbanking-us.yaml` L2440-2465 (`GetAllCustomerTransactions`).
- `GET /aggregation/v4/customers/{customerId}/accounts/{accountId}/transactions` —
  single account, same params plus `showDailyBalance` — L2508-2545
  (`GetCustomerAccountTransactions`).

Both cap at 1000 transactions per page ("no limit for the size of the
window between `fromDate` and `toDate`, however, the maximum number of
transactions returned on one page is 1000" — same spec text repeated on
both operations).

**Modified/deleted detection** is carried by a tri-state `status` field,
not a delta/cursor mechanism — developer.mastercard.com/open-finance-us/documentation/products/manage/transaction-data/understanding-transaction-data/#transaction-status:

- `active` — currently present in the institution's data as of the most
  recent refresh.
- `pending` — initiated but not yet posted; short-lived; "there is no
  continuity guarantee for transactions to move from pending to active"
  — a pending transaction may update in place to `active`, or may be
  marked `shadow` while a **new** transaction record (new id) appears for
  the posted version. Pending transactions "can only change to active or
  be removed from the response, they cannot change to shadow" directly.
- `shadow` — previously reported as `active` in an earlier aggregation,
  no longer present in the institution's current data (Mastercard's
  own soft-delete signal). Mastercard's explicit recommendation: "in
  most cases we recommend that you treat shadow transactions as though
  they have been deleted."

**History depth**: standard aggregation returns "up to 180 days of
transactions prior to the date each account was added to the Finicity
system" (`GetAllCustomerTransactions` description, L2450-2452). Deeper
backfill is a separate, explicitly billable endpoint:
`POST /aggregation/v1/customers/{customerId}/accounts/{accountId}/transactions/historic`
— "load up to 24 months of historic transactions for the account...
This is a premium service. The billable event is a call to this service
specifying a customer ID that has not been seen before by this service"
(billed once per customer, not per account/call) —
`openbanking-us.yaml` L1115-1166 (`LoadHistoricTransactionsForCustomerAccount`).
This is Finicity's analogue of MX's separately-billed "Extended
Transaction History."

## 6. Webhooks

Finicity/Mastercard runs **three parallel, differently-signed webhook
mechanisms**, and is mid-migration to a fourth unified one:

1. **Mastercard Data Connect Webhooks** — session-progress events during
   a Connect flow (see §3). Signed via `X-Finicity-Signature` = HMAC-SHA256
   of the raw request body, keyed with the **Partner Secret** itself —
   developer.mastercard.com/open-finance-us/documentation/webhooks/webhooks-connect/#prevent-spoofing.
   Non-200 responses are retried for 3 days with a documented
   exponential-backoff schedule (12ms, 72ms, 432ms, 2.6s, 15.6s, 93s,
   then hourly).
2. **TxPUSH** — account/transaction change notifications, entirely
   separate from Data Connect webhooks (see §5/§7 below for the
   subscribe flow). Signed via a **different** scheme: `txpush-signature`
   = HMAC-SHA256 of a constructed signing string (`content-type` header
   value + `host` header value + Base64-encoded body), keyed with a
   **per-subscription `signingKey`** issued when the subscription is
   first verified (not the Partner Secret) —
   developer.mastercard.com/open-finance-us/documentation/products/manage/tx-push/#validating-a-txpush-signature.
   Unacknowledged notifications are resent every 30 minutes for up to 6
   hours, then cancelled.
3. **Report Webhooks** — separate family for Lend-product report
   generation (started/complete/failed) — out of scope for Pinch's
   accounts/transactions use case, not researched further.
4. **OBWMS (Open Banking Webhook Management System)** — a newer,
   centralized subscription API (`/notification-subscriptions/webhooks/*`)
   intended to unify webhook handling across services, requiring a
   downloaded "Mastercard signature verification key" from the project
   dashboard — developer.mastercard.com/open-finance-us/documentation/onboarding/#obwms-authentication.
   Confusingly, **the OBWMS endpoints found in the current OpenAPI spec
   are themselves marked `deprecated: true`**, each carrying the note:
   "*The OBWMS endpoints will be replaced by a new standalone service
   during 2026. Speak to your Customer Service Manager for details.*" —
   `openbanking-us.yaml` L4046-4048 and repeated on every OBWMS operation
   (L4064-4440). The Webhook Notifications overview page corroborates
   this is in-flight: "Existing services will be migrated to the Open
   Banking Webhook Management System during 2026" —
   developer.mastercard.com/open-finance-us/documentation/webhooks/.
   **Net effect: as of this research, Finicity's webhook subsystem is a
   moving target** — flagged in "What I could not verify."

## 7. Aggregation model

`POST /aggregation/v1/customers/{customerId}/accounts` (and the
institution-login-scoped variant) triggers a refresh: "recommended
timeout... 180 seconds... you can terminate the connection after making
the call, the operation will still complete... pull the account records
to check for an updated `aggregationAttemptDate`" —
`openbanking-us.yaml` L727-747 (`RefreshCustomerAccounts`). "Active
accounts are automatically refreshed by Finicity once per day." Manual
refresh is explicitly discouraged for routine use: "client apps are not
permitted to automate calls to the Refresh services... apps may call
Refresh services... when there is a specific business case... discuss
with your account manager" (same text repeated on every refresh
operation).

No numeric global rate limit was found in any fetched page — the
mechanism is confirmed but not quantified: the onboarding page states
"the App Key controls the rate limit, and invalid keys have a rate limit
of 0" (implying valid keys have a partner-specific, non-public limit) —
developer.mastercard.com/open-finance-us/documentation/onboarding/#too-many-requests-error.
A second, per-institution throttle exists independently: error code
`977`, HTTP 500, "Service has reached the maximum limit or expired,"
common cause "the number of requests to a particular financial
institution exceeds the set rate limit" —
developer.mastercard.com/open-finance-us/documentation/errors/request/max-limit-exceeded/#977.
Neither page discloses an actual requests-per-minute/day number.

**Data Access Tiers** is a separate billing-model overlay (not a
different sync mechanism): Tier 1 "Account Simple Details" (free), Tier
2 "Account Full Details" (paid, balances only), Tier 3 "Account &
Transaction Details" (paid, adds ~6 months of transactions) — a
per-customer-per-month billing construct, gated behind a Sales
Representative conversation, and **explicitly incompatible with TxPUSH**
("Partners using TxPUSH are not eligible for Data Access Tiers") —
developer.mastercard.com/open-finance-us/documentation/products/manage/data-access-tiers/.
This is a monetization detail, not a schema concern — flagged for
completeness since it affects which refresh endpoint variant
(`RefreshCustomerAccountsByInstitutionLogin` vs. the `V2` "for Data
Access Tiers" sibling) an integration would use.

## 8. Investments

Finicity does **not** have a separate Investments product surface at
all — no `Investments` tag exists in the OpenAPI spec's tag list
(`openbanking-us.yaml` L1-98), unlike Plaid (`/investments/*`) or even
MX (a dedicated Holdings endpoint family). Instead:

- **Holdings** are an embedded array directly on the `Account` object:
  `position: {type: array, items: $ref: CustomerAccountPosition,
  description: "Investment holdings"}` — L41056-41060. There is no
  separate holdings-list endpoint; holdings simply ride along whenever an
  account is fetched via the standard Accounts endpoints (the Quick Start
  Guide's own sample `RefreshCustomerAccounts` response includes a
  `type: "roth"` and a `type: "investmentTaxDeferred"` account, each with
  a `detail` object carrying `marginBalance`, `availableCashBalance`,
  `vestedBalance`, `currentLoanBalance`).
- **Investment transactions** are likewise **not** a separate endpoint —
  they flow through the identical `GetAllCustomerTransactions` /
  `GetCustomerAccountTransactions` endpoints as every other transaction,
  distinguished only by an `investmentTransactionType` field (example
  value: `dividend`) appearing on the transaction record —
  `openbanking-us.yaml` L17400-17447.

Net: Finicity's investments story is the most "folded into the base
primitives" of the three providers researched so far — no separate
resource type, no separate identifier, nothing that would need its own
schema seam.

## 9. Amount / sign conventions

Sign convention is **segment-dependent**, called out explicitly on
developer.mastercard.com/open-finance-us/documentation/products/manage/transaction-data/understanding-transaction-data/#transaction-data-by-segment:

> "There is one important difference in the way inflows and outflows are
> represented for different account types: in traditional transaction
> data such as for a checking account, a positive amount (credit)
> corresponds to inflow (salary), while a negative amount corresponds to
> outflow (paying a bill). However, with investment account or credit
> card transaction data this convention is the opposite: inflows are
> negative (stock sales / paying down a balance on credit card) and
> outflows are positive (stock purchases / purchase on a credit card)."

So: deposit accounts (checking/savings/money-market) use the intuitive
sign (positive = money in); investment accounts **and** credit-card/
line-of-credit accounts invert it (negative = money in, e.g. a card
payment reducing the balance). This is a real modeling gotcha — any
Pinch ingestion logic that assumes one global sign rule across account
types would misclassify investment and credit-card transactions for
Finicity, unlike MX's model (magnitude + separate `CREDIT`/`DEBIT` type
enum, no sign flip needed) or Plaid's (also a single global sign
convention). A `type` field (values seen: `"debit"`) appears in at least
one sample payload as a secondary signal, but its reliability across all
institutions/account types was not confirmed in the fetched pages.

## 10. Developer / sandbox access

Fully self-serve and free for mock-data testing: "Log in or sign up to
Mastercard Developers and select Create New Project... Select Open
Finance from the Select your API service drop-down list... From the
Credentials section, click Sandbox to view the Sandbox credentials:
Partner ID, Secret, and App Key" —
developer.mastercard.com/open-finance-us/documentation/quick-start-guide/#create-a-sandbox-project.
This puts the project on the **Test Drive plan**: "a non-billable,
unlimited plan that provides access to all Open Finance API endpoints"
against a mock bank ("FinBank") using testing customer records — no
contract, no sales conversation, no live institution data —
developer.mastercard.com/open-finance-us/documentation/onboarding/#sandbox.

Getting real institution data requires "Moving to Production" — clicking
"Request Production access," which offers a choice of:

- **Test Drive Premium** — "a free, 30-day limited-duration plan that
  grants you access to all Open Finance API endpoints... allows you to
  test your integration with live financial data before fully moving to
  the Production plan," auto-converting to billable Production after 30
  days.
- **Production (billable)** — scoped to whatever endpoints are in the
  signed contract.

Both require Mastercard's approval and a signed agreement: "Access to
these plans will only be approved if a contract between you and
Mastercard has been executed. Work with your Sales Representative or
Client Success Manager" —
developer.mastercard.com/open-finance-us/documentation/onboarding/#moving-to-production.
This is the same "free sandbox, sales-gated production" pattern found
for MX, made explicit and contractual on Mastercard's side rather than
merely implied.

**Region**: "Requests to the Open Finance US APIs must originate from an
IP address in the United States, United Kingdom, Canada or Australia" —
Quick Start Guide, Step 1 note — a caller-origin restriction, not a
coverage restriction; every endpoint description in the spec is
individually tagged "_Supported regions_: 🇺🇸" for this specific product
line (`open-finance-us`; a separate `open-finance-au` product exists for
Australia and was not researched).

## 11. THE KEY QUESTION — does the schema fit with zero changes?

**Yes.** Mapping Finicity onto Pinch's provider-agnostic shapes:

- **Per-(provider, ledger) enrollment** → Finicity's `customerId`,
  returned once from `AddCustomer`/`AddTestingCustomer` (§2) and reused
  for every subsequent call for that end-user. Clean fit, same shape as
  MX's `user_guid`.
- **`Connection.provider` + `Connection.provider_item_id`** →
  `institutionLoginId` (§4), the grouping key for one bank login's worth
  of accounts. It is a numeric value on the wire but exposed as a string
  in path parameters — fits `provider_item_id: str` with no coercion
  concerns (same pattern as storing Plaid's `item_id` or MX's
  `member_guid` as strings).
- **`Connection.encrypted_secret` (nullable)** → stays `NULL` for
  Finicity, same as MX. Nothing in the Customer schema, the Account
  schema, or the Connect response (`ConnectUrl {link}`) hands back a
  per-connection bearer secret the integrator must store. Every call
  after Connect completes is authenticated with the **same** static
  `Finicity-App-Key` + rotating `Finicity-App-Token` pair used for every
  other customer/connection under that partner — those live in settings
  exactly like Plaid's `client_id`/`secret` or MX's `client_id`/`api_key`,
  not per-connection.

**No third shape found.** No required per-connection API key, and no
separate "account group" identifier that fails to map onto
`Connection`/`provider_item_id` — `institutionLoginId` **is** that
identifier, end to end.

**One non-blocking nuance, not a schema blocker**: `institutionLoginId`
is reassignable by Finicity itself when it migrates an institution from
legacy screen-scraping to OAuth (`MigrateInstitutionLoginAccounts`, §4)
— a laxer stability guarantee than either Plaid's `item_id` or MX's
`member_guid`, neither of which documents an equivalent
provider-initiated reassignment. This doesn't require a schema change
(the existing `provider_item_id` column can simply be updated in place
on the same `Connection` row, the same way Pinch would already need to
handle a Plaid re-link producing a new `item_id`), but it is a real
operational case a Finicity integration would need a reconciliation path
for that a Plaid- or MX-only integration doesn't currently need.

## Coverage: Synchrony Bank / store cards & NewRez / Shellpoint mortgage

Unlike MX (institution search is auth-gated) and closer to Plaid's public
`plaid.com/institutions` pages, Mastercard publishes a **public,
unauthenticated, live-searchable** institution table at
developer.mastercard.com/open-finance-us/documentation/financial-institution/supported-institutions/ —
"The following table lists the Financial Institutions (FIs) that have
gone through our certification process, showing which products each FI
supports. Use the search box to find particular FIs." The page itself
warns the public list "might not match the list returned when calling
the Get Institutions API endpoint yourself... some FIs require
additional onboarding with each partner" — so this is a strong signal,
not a guaranteed match to what Pinch would see with real credentials.

Live searches against that table (2026-08-04), reading each row's
per-product checkmark (columns: AO/ABC/**TA**/VOI/VOA/ACH/SA/AHA/LPD/SLD
— **TA** = Transaction Aggregation, the product Pinch needs):

- **Synchrony Bank** (ID `101955`) — certified for **ABC** (Account
  Balance Check) and **AHA** (Account History Aggregation) only. **Not**
  certified for TA (Transaction Aggregation) at the parent-bank level.
- **Amazon.com Store Card** (ID `100013`) — **TA certified**. A separate
  institution entry from the parent bank, same pattern Plaid uses.
- **Lowes Consumer Credit Card** (ID `3307`) — **TA certified**. Note:
  searching the literal string `Lowe's` (with the apostrophe) returns
  **zero results**; searching `Lowes` (no apostrophe) works — a
  literal-substring search quirk in Mastercard's own tool. There is also
  a distinct **Lowes Canada - Syncrhony Credit Card** (ID `178712`,
  Mastercard's own typo of "Synchrony," TA certified) — a Canadian
  variant, not relevant to Pinch's US ledgers.
- Dozens of other co-branded Synchrony store cards (Walmart, eBay,
  Verizon, Google, Gap, Athleta, BP, Harbor Freight, etc.) all appear as
  separate TA-certified entries under a `Synchrony` search — same
  fan-out pattern as Plaid.
- **Shellpoint Mortgage Servicing** (ID `101645`) — **TA certified**.
  Searching **`NewRez`** (the brand Plaid's institution search actually
  matches) returns **zero results** on Finicity's table. This is the
  **exact inverse** of the Plaid finding, where "Shellpoint" returned
  zero and "NewRez" was the working search term — a genuine, easy-to-get
  backwards pitfall if Pinch ever surfaces provider-specific institution
  search hints to users across both providers.

Net: on this public table, every institution Plaid was found to be
having availability problems with (Synchrony store cards, the mortgage
servicer) has a certified Finicity/Mastercard counterpart supporting
Transaction Aggregation — a genuinely promising signal for Finicity as a
resilience-motivated third provider, though unverified against a live
account (see "What I could not verify").

## What I could not verify

- **Whether the public Supported Institutions table matches what a real
  Partner ID would see from `GET /institution/v2/institutions`** — the
  page itself warns it might not ("some FIs require additional
  onboarding with each partner"). No Finicity credentials exist for this
  project, so the authenticated `GetInstitutions`/`GetCertifiedInstitutions`
  endpoints could not be queried directly, unlike the sibling Plaid
  research which had production access.
- **Exact numeric rate limits** — both the App-Key-scoped limit and the
  per-institution throttle (error `977`) are confirmed mechanisms with no
  published number, the same shape of gap as the sibling MX file.
- **OBWMS's actual current state** — the OpenAPI spec marks the OBWMS
  endpoints themselves `deprecated: true` in favor of "a new standalone
  service during 2026," while the prose docs describe OBWMS as the
  forward-looking replacement for the older per-product webhook systems.
  Whether an integration should target OBWMS, the legacy per-product
  webhooks, or wait for the next service entirely is a moving target
  during this exact research window and was not resolved from primary
  sources alone.
- **Reliability of the `type` (`debit`/`credit`) field** as a secondary
  signal to the segment-dependent sign convention (§9) — only confirmed
  present in one sample payload; not confirmed as populated across all
  institutions/account types.
- **Exact `username` uniqueness / one-customer-per-end-user enforcement**
  — no fetched page states what happens if the same real person is
  enrolled twice, or whether `username` must be unique per Partner ID.
- **Whether Test Drive sandbox signup requires payment info** — the
  Quick Start Guide shows no paywall or billing step before Sandbox
  credentials are issued, but this wasn't independently confirmed by
  actually creating a project.
- **The "500 test customers" and other Test-Drive numeric ceilings** —
  sourced only from the site's own `llms-full.txt` AI-summary page, not
  a prose documentation page directly; treat as lower-confidence than
  the OpenAPI-spec-sourced facts in this file.
- **The `Auth Type: OAuth1.0a` tag** surfaced once via a WebSearch
  snippet of Mastercard's own service-catalog metadata — contradicts
  everything else found about the Partner-ID/Secret → App-Token scheme,
  and was not resolved either way from a primary technical source.
- **Whether Synchrony Bank's missing TA certification at the parent-bank
  level (ABC/AHA only) has any practical effect** for Pinch, given real
  users would search for their card's brand name (Amazon, Lowes, etc.,
  all separately TA-certified) rather than "Synchrony Bank" itself —
  plausible but not confirmed against an actual Connect flow.
- No PRODUCTION-EMPIRICAL section exists in this file at all, matching
  the sibling MX research and unlike the sibling Plaid research —
  everything above is a documentation claim (backed, in most cases, by
  Mastercard's own published OpenAPI spec rather than prose alone, which
  is a stronger source than the sibling MX file had access to), not a
  live-tested result, and should be treated accordingly until an actual
  Finicity/Mastercard Sandbox or Production account is provisioned and
  exercised.
