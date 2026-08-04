# MX Platform API — primary-source due diligence (researched 2026-08-04)

Why: evaluating MX as a second `SyncProvider` alongside Plaid. Motivated
by [docs/research/plaid-synchrony-newrez-coverage.md](./plaid-synchrony-newrez-coverage.md),
which found Plaid-side outages/degradation blocking Synchrony store
cards (Lowe's Advantage, Amazon Store Card) and near-unusable NewRez
mortgage connectivity — not a Pinch integration bug, but a live
availability gap on Plaid's network. This file asks: is MX's Platform
API (docs.mx.com) a viable second provider, and does it independently
cover the institutions Plaid is currently failing on?

## Sources

Primary sources only, fetched directly:

- **docs.mx.com** — Platform API reference pages (`/api-reference/platform-api/reference/*`),
  product docs (`/products/connectivity/*`), Connect widget docs
  (`/connect/*`), webhook docs (`/resources/webhooks/*`), and the
  site's own LLM-oriented sitemap at `docs.mx.com/llms.txt`, which was
  used to enumerate real endpoint paths before fetching each one
  individually (this avoided guessing at paths).
- **mx.com** — MX's own press/news pages (`mx.com/news/*`), used only
  for the institution-count marketing claim, since docs.mx.com does not
  publish that figure.
- **dashboard.mx.com/sign_up** — MX's own developer signup page, for
  free-tier terms.
- No third-party blogs, Stack Overflow, or secondary write-ups were
  used as evidentiary sources. `openbankingtracker.com` and similar
  aggregator sites turned up in search results but were **not** cited
  — they're not primary and added nothing docs.mx.com didn't already
  say. There is no PRODUCTION-EMPIRICAL section in this file (unlike
  the sibling Plaid research) — **Pinch has no MX credentials**, so
  nothing here was verified against a live MX account; this is
  documentation-only due diligence, called out explicitly in every
  answer where that matters and again in "What I could not verify."

## TL;DR

| # | Question | Answer | Cite |
| - | -------- | ------ | ---- |
| 1 | Connect flow | `POST /users/{user_id}/widget_urls` with `widget_type: "connect_widget"` returns a short-lived hosted URL (expires 10 min / first use). No token exchange — the widget hands back a `member_guid` via `postMessage` (`mx/connect/memberConnected`); backend then just uses its `client_id`/`api_key` + `user_guid`/`member_guid` for everything after. No Plaid-style `access_token`. | docs.mx.com/api-reference/platform-api/reference/request-widget-url, docs.mx.com/connect/widget-events |
| 2 | Data model | `user` → `member` (one per institution relationship) → `account`(s). `member_guid` is the connection identifier. MX stores/manages credentials server-side (OAuth or password); the integrator never holds bank credentials or an access token. | docs.mx.com/api-reference/platform-api/reference/members |
| 3 | Sync semantics | No cursor-sync endpoint like `/transactions/sync`. `GET /users/{id}/transactions` is date-range + offset pagination (`from_date`/`to_date`, `page`/`records_per_page`, plus `from_updated_at`/`to_updated_at`). Default history is **90 days**; a separate paid "Extended Transaction History" product/job gets 24 months. Deletions are **not** visible via the list endpoint — only via the Transactions **webhook**'s `deleted` action. | docs.mx.com/api-reference/platform-api/reference/list-transactions, docs.mx.com/products/connectivity/account-aggregation, docs.mx.com/products/connectivity/extended-transaction-history, docs.mx.com/resources/webhooks/transactions |
| 4 | Webhooks | ~20+ event families (Aggregation, Connection Status, Transactions, Members, Holdings, Initial Data Ready, Job Status, etc.), configured per-**client** in the Client Dashboard (not per-user via API); unlisted webhooks need an MX Support ticket to provision. No documented HMAC/signature-verification header — only transport-level options (Basic Auth / mTLS / OAuth2 client-credentials) configurable per webhook URL. Payload richness **varies by webhook**: Aggregation (`member_data_updated`) is pointer-only (counts + guids); Transactions and Holdings webhooks carry the full object inline. | docs.mx.com/resources/webhooks, docs.mx.com/resources/webhooks/aggregation, docs.mx.com/resources/webhooks/transactions |
| 5 | Aggregation model | Background aggregation runs automatically ~every 24h per member (min 20h since last run). On-demand `POST .../aggregate` is throttled: cannot re-run within **3 hours** of a successful aggregation. Full `connection_status` enum found (19 values incl. `CONNECTED`, `CHALLENGED`, `DENIED`, `DELAYED`, `IMPAIRED`, `DEGRADED`, `DISCONNECTED`, etc.). MFA surfaces as `CHALLENGED` + a `resume` endpoint that takes challenge answers. | docs.mx.com/products/connectivity/account-aggregation, docs.mx.com/api-reference/platform-api/reference/members, docs.mx.com/api-reference/platform-api/reference/resume-aggregation |
| 6 | Coverage | **Could not verify Synchrony/NewRez presence empirically** — `GET /institutions` (search-by-`name`) requires authenticated API access Pinch doesn't have, and no public no-auth MX institution directory (analogous to plaid.com/institutions) was found. MX's own "16,000 institutions" figure is **inconsistent across MX's own press pages**: 16,000 (Oct 2021) vs. 13,000 (May 2023) vs. an unrelated "2,000 institutions powered" framing (Aug 2020) — not a single stable verified number. | docs.mx.com/api-reference/platform-api/reference/list-institutions, mx.com/news/mx-introduces-m-xdata-for-business, mx.com/news/mx-delivers-free-data-access-solution |
| 7 | Free dev tier | Free account: dashboard + dev environment, "up to 100 users at some of the top financial institutions" (**live** aggregation, not sandbox-only), transactions/balances/account-info + enhancement/categorization included. Investment holdings ("Investment Data Enhancement") is explicitly a **separate billable** product even for otherwise-free usage. Identity ("Account Owner Identification") tier status not confirmed in docs. | dashboard.mx.com/sign_up, docs.mx.com/api-reference/platform-api/reference/investment-holdings |
| 8 | Investments | Holdings only — `list-holdings-by-{account,member,user}`, `read-holding`. **No investment-transactions endpoint or webhook found** in MX's own sitemap or docs (unlike Plaid, which has `/investments/transactions/get`). This is a documentation-absence finding, not an explicit "we don't have this" statement from MX. | docs.mx.com/api-reference/platform-api/reference/investment-holdings, docs.mx.com/llms.txt |
| 9 | Amount/sign convention | `amount` is a `Decimal` (dollars, not integer cents) with **no explicit sign documented** in the Platform API transaction schema — direction is carried by a separate `type` enum (`CREDIT`/`DEBIT`), plus an `is_expense` boolean. `balance`/`available_balance` are also `Decimal` dollars. | docs.mx.com/api-reference/platform-api/reference/transactions, docs.mx.com/api-reference/platform-api/reference/accounts |
| 10 | Pending transactions | `status` enum: `POSTED` / `PENDING` (nullable in edge cases). MX *attempts* to preserve the same `guid` when a pending transaction posts; if matching fails, the pending record is deleted and a **new** posted record (new guid) is created instead. All `PENDING` transactions are hard-deleted after 14 days as a failsafe regardless. | docs.mx.com/api-reference/platform-api/reference/transactions |

## 1. Connect flow

A backend requests a hosted widget URL with:

```
POST /users/{user_id}/widget_urls
Authorization: Basic base64(client_id:api_key)
Accept-Version: v20250224

{
  "widget_url": {
    "widget_type": "connect_widget",
    "mode": "aggregation",
    "color_scheme": "light",
    "data_request": { "products": ["transactions", "account_verification"] }
  }
}
```

Response is a single hosted URL (`int-widgets.moneydesktop.com/md/connect/...`)
that "expires after ten minutes or upon first use, whichever occurs
first" — docs.mx.com/api-reference/platform-api/reference/request-widget-url.
Auth throughout is HTTP Basic with base64 `client_id:api_key`, no
OAuth-style bearer/refresh token cycle for the backend-to-MX leg.

The widget itself communicates back to the host page via `postMessage`
events under the `mx/connect/*` namespace. The success event is
**`mx/connect/memberConnected`**, fired when "a member has
successfully connected and the data you requested in your Widget URL
request has finished aggregating," carrying `user_guid`, `session_guid`,
and `member_guid`. Related events: `mx/connect/memberStatusUpdate`
(carries `connection_status`), `mx/connect/memberError`,
`mx/connect/memberDeleted`, `mx/connect/submitMFA` /
`updateCredentials` (mid-flow), and OAuth-specific events for
WebView integrations — docs.mx.com/connect/widget-events.

**No token-exchange step exists.** Unlike Plaid's `public_token` →
`access_token` exchange, MX hands back a `member_guid` directly and the
backend thereafter authenticates every call with the same
`client_id`/`api_key` pair plus the relevant `user_guid`/`member_guid`
path segments — there is no separate per-connection secret the
backend has to mint or store. (MX does have a distinct `SSO API v3`
for other widget types — e.g. PFM/Financial Insights widgets — but
that's a different product surface, not part of the Connect flow;
docs.mx.com/api-reference/sso/v3.)

## 2. Data model

Three core resources relevant here (a fourth, `holding`, covered in §8):

- **`user`** — represents the end user; MX assigns a `guid`, the
  integrator can also set its own `id` and map between the two.
- **`member`** — represents *one user's relationship with one
  institution*. This is the connection object; its `guid` (e.g.
  `MBR-48d9a481-...`) is what the rest of the API keys off of. A
  single user commonly has multiple members (bank, mortgage servicer,
  card issuer, etc.) — docs.mx.com/api-reference/platform-api/reference/members.
- **`account`** — one or more financial accounts under a member, each
  with its own `guid` (e.g. `ACT-8e6f92c8-...`).

Per docs.mx.com/api-reference/platform-api/reference/members, MX
stores/manages the actual bank credentials (password-based or OAuth)
on its own side as part of running the aggregation; nothing in the
member/account schema exposes a credential or an access-token-like
secret to the integrator. The integrator's only durable references
are `user_guid` and `member_guid`, used together with the static
`client_id`/`api_key` for every subsequent call — there is no
per-connection bearer token analogous to Plaid's `access_token` to
store securely.

`member.connection_status` full enum (docs.mx.com/api-reference/platform-api/reference/members):
`CREATED`, `CONNECTED`, `CHALLENGED`, `DENIED`, `PREVENTED`,
`REJECTED`, `LOCKED`, `IMPEDED`, `IMPAIRED`, `DEGRADED`, `DELAYED`,
`FAILED`, `DISCONNECTED`, `EXPIRED`, `CLOSED`, `DISCONTINUED`,
`DISABLED`, `IMPORTED`, `PENDING`.

## 3. Transaction sync semantics

`GET /users/{user_id}/transactions` (docs.mx.com/api-reference/platform-api/reference/list-transactions):

| Param | Default | Notes |
| --- | --- | --- |
| `from_date` | 120 days ago (endpoint default window) | Unix timestamp |
| `to_date` | 5 days forward | Unix timestamp |
| `page` | 1 | |
| `records_per_page` | 25 | 10–1000, values >1000 silently revert to 25 |
| `from_updated_at` / `to_updated_at` | — | filters by MX's internal update time, closest thing to change-detection on this endpoint |
| `includes` | — | opt into `repeating_transactions,merchants,classifications,geolocations` |

This is **pure date-range + offset pagination** — there is no
cursor/`next_cursor` token comparable to Plaid's `/transactions/sync`.
`from_updated_at`/`to_updated_at` let an integrator poll for recently
touched rows, but **deleted** transactions do not show up in this list
endpoint at all (they just stop appearing); the only documented way to
learn about a deletion is the Transactions **webhook**'s `deleted`
action (see §4) — docs.mx.com/resources/webhooks/transactions.

Default history depth for a first aggregation is **90 days** —
"Account Aggregation enables connection to retrieve 90 days of data
for their accounts and transactions" —
docs.mx.com/products/connectivity/account-aggregation. Deeper backfill
is a distinct, separately-named product: **Extended Transaction
History**, which pulls "up to 24 months" of account/transaction data
and is described as a "premium aggregation-type job that won't start
the [3-hour] throttle period" that applies to ordinary on-demand
aggregation — docs.mx.com/products/connectivity/extended-transaction-history.
The exact triggering mechanism (special `aggregate` job-type parameter
vs. a distinct endpoint) was not pinned down beyond that description;
flagged in "What I could not verify."

## 4. Webhooks

Configuration is **client-level**, via the Client Dashboard
("Developers" → "Webhooks"); webhook types not already visible in the
dashboard require a support ticket to MX (client ID, environment,
target URL, security requirements) — docs.mx.com/resources/webhooks.
There is no documented per-user/per-member API for registering
webhooks — it's a single target-URL-per-event-type client
configuration, optionally one URL per event type.

Event families found (docs.mx.com/resources/webhooks and its
per-type sub-pages): Accounts, **Aggregation**, Balance, Budgets,
Categories, **Connection Status**, Goals, **History**, **Holdings**,
**Initial Data Ready**, Insights, **Job Status**, **Members**,
Microdeposits, Notifications, Spending Plan, Statement, Tags and
Taggings, **Transactions**, Users, Verification. ("Beats" is called
out as deprecated.) The doc adds a caveat specific to Platform API
clients: "only the aggregation, balance, connection status, history,
insights, and statements webhooks are available" for that surface —
i.e. not every listed event family is necessarily reachable depending
on which MX product surface a client is on.

Payload richness is **not uniform**:

- **Aggregation** (`member_data_updated`, type `AGGREGATION`) is
  pointer/count-only — `member_guid`, `job_guid`,
  `transactions_created_count`, `transactions_updated_count`, no
  actual transaction data — docs.mx.com/resources/webhooks/aggregation.
- **Transactions** webhook carries the **full transaction object**
  inline under an `action` of `created`/`updated`/`deleted` — "notify
  you when a transaction is created, updated, or deleted in the MX
  system for any user in the client" — docs.mx.com/resources/webhooks/transactions.
- **Holdings** webhook similarly carries the full holding object with
  `action` of `created`/`updated`/`deleted` —
  docs.mx.com/resources/webhooks/holdings.
- **Connection Status** webhook fires with `action: "CHANGED"` for
  eight "actionable" statuses (`CHALLENGED`, `DENIED`, `EXPIRED`,
  `IMPAIRED`, `IMPEDED`, `LOCKED`, `PREVENTED`, `REJECTED`), and
  includes a human-readable `connection_status_message` —
  docs.mx.com/resources/webhooks/connection-status.
- **Initial Data Ready** (`initial_data_ready`) is a thin
  guid-only pointer fired once priority data from the first
  aggregation is ready to fetch — docs.mx.com/resources/webhooks/initial-data-ready.

**Signature verification: not found.** No page in the webhooks
section documents an HMAC header, a signing secret, or any
cryptographic payload-signature mechanism. The only security controls
documented are transport/endpoint-level: "all webhook requests are
encrypted by default" (TLS) plus optional HTTP Basic Auth, Mutual TLS,
or OAuth2 client-credentials **on the receiving webhook URL itself** —
docs.mx.com/resources/webhooks. This is materially different from
Plaid's model and is called out explicitly in "What I could not
verify" since a name like `X-MX-Signature` was searched for and not
found in any primary source — it may not exist.

## 5. Aggregation model

Background aggregation is automatic: "MX automatically aggregates each
`member` approximately every 24 hours," gated on the member being
`CONNECTED`/`CREATED`/`UPDATED` (not disabled), the source supporting
background aggregation, and the member not having aggregated within
the last 20 hours — docs.mx.com/products/connectivity/account-aggregation.

On-demand aggregation — `POST /users/{user_id}/members/{member_id}/aggregate`
— is throttled: "you cannot run a new aggregation within three hours
of a successful aggregation," whether the prior run was foreground or
background — same page. `POST /users/{user_id}/members/aggregate_all`
exists to kick every member for a user at once, restricted to members
created with `use_cases: PFM` or it 403s —
docs.mx.com/api-reference/platform-api/reference/aggregate-all-members.
No numeric per-hour/day API rate limit (as opposed to the per-member
3-hour cooldown) was found in the fetched pages.

MFA / reauthentication surfaces as `connection_status: CHALLENGED`,
delivered both synchronously (poll/read the member) and via the
Connection Status webhook. The integrator collects answers and submits
them via `PUT /users/{user_id}/members/{member_id}/resume` with a
`challenges: [{guid, value}]` array; response is `202` with the
member's updated status, typically transitioning toward `CONNECTED` —
docs.mx.com/api-reference/platform-api/reference/resume-aggregation.
Full status enum is listed under §2 above.

## 6. Coverage (Synchrony / NewRez / general institution count)

**Could not be verified empirically.** MX exposes `GET /institutions`
with a `name` search param and a `supported_products[]` filter
(values seen: `account_verification`, `identity_verification`,
`transactions`, `transaction_history`, `statements`, `investments`,
`rewards`) — docs.mx.com/api-reference/platform-api/reference/list-institutions
— but it requires authenticated `client_id`/`api_key` Basic-Auth
credentials. **Pinch does not currently have an MX developer account**,
so this endpoint could not be queried for "Synchrony" or
"NewRez"/"Shellpoint" the way the sibling Plaid research probed
`/institutions/search` with production credentials. No public,
unauthenticated MX institution-directory page (an MX analogue of
plaid.com/institutions) was found via search — MX's public marketing
site does not appear to expose a queryable bank list.

On the oft-cited "~16,000 institutions" figure: **MX's own numbers are
inconsistent across its own press pages**, and this file deliberately
does not just repeat the marketing line:

- Oct 20, 2021: "MX connects more than 16,000 financial institutions
  and fintechs" — mx.com/news/mx-introduces-m-xdata-for-business.
- May 10, 2023: "MX connects more than **13,000** financial
  institutions and fintechs" (same phrasing, lower number) —
  mx.com/news/mx-delivers-free-data-access-solution.
- Aug 20, 2020: a different framing entirely — "powering more than
  2,000 financial institutions" (institutions MX's *software* runs
  inside, not institutions it aggregates *from* — a different metric)
  — mx.com/news/mx-widens-lead-with-more-than-connections-to-the-top-financial-institutions-and-fintechs.

Read together, MX's "connects N institutions" figure moved **down**
between 2021 and 2023 in MX's own materials (16,000 → 13,000), which
undercuts treating either number as a stable, current fact — and
neither figure is corroborated by any docs.mx.com engineering page
(the API reference pages never state a total count). Given Plaid's
own institution catalog is on the order of magnitude of ~12,000+ and
the two networks are known in the industry to have limited overlap in
long-tail/niche institutions (uncorroborated industry generalization,
not sourced here), coverage of Synchrony's screen-scraped store-card
network and NewRez/Shellpoint specifically is an open question that
can only be answered with an actual MX sandbox/dev account and a live
`/institutions?name=synchrony` / `?name=newrez` / `?name=shellpoint`
query — which is the natural next step before committing to MX as a
second provider.

## 7. Free developer tier

dashboard.mx.com/sign_up states free accounts include: "Access to the
MX developer environment and our powerful dashboard," data
aggregation for "transactions, balances, and account information,"
support for **"up to 100 users at some of the top financial
institutions"** (i.e. **live** aggregation against real institutions,
not a sandbox-only cap), and transaction enhancement/categorization.
This is a materially different free-tier shape than Plaid's (Plaid's
free "Sandbox" is fake data only; production access requires a paid
plan or approval) — MX's free tier appears to permit limited *real*
production usage.

Gating: Investment holdings (branded "Investment Data Enhancement") is
explicitly called out as a **separate, billable** product — "a user is
automatically enrolled when you use any of the Read/List [holdings]
endpoints for that user," implying per-use billing distinct from the
free-tier transactions/balances bundle —
docs.mx.com/api-reference/platform-api/reference/investment-holdings,
docs.mx.com/products/data/investment-data-enhancement. Identity/Account
Owner Identification's tier status (bundled free vs. gated) was **not
confirmed** in any fetched primary source — flagged below.

## 8. Investments

MX's investments surface is **holdings only**. Endpoints per MX's own
sitemap (docs.mx.com/llms.txt) and the Investment Holdings overview
(docs.mx.com/api-reference/platform-api/reference/investment-holdings):
`read-holding`, `list-holdings-by-account`, `list-holdings-by-member`,
`list-holdings-by-user`, plus a non-billable `deactivate-user` call to
remove a user from the (billable) product. The holding object tracks
30+ fields (equities, fixed income, options, mutual funds,
alternatives, vesting, unrealized gains/losses, current pricing).

**No investment-transactions endpoint or webhook was found anywhere in
MX's documentation** — neither in the sitemap nor in any fetched page.
This contrasts with Plaid, which has a dedicated
`/investments/transactions/get`. This is a documentation-absence
finding (MX never explicitly says "we don't offer this"), not a
positive confirmation of non-existence — flagged in "What I could not
verify," but the absence from MX's own generated sitemap of doc pages
is reasonably strong negative evidence.

## 9. Amount / sign conventions

Transaction `amount` is typed **`Decimal`** — dollars (and cents as a
fractional part), not Plaid-style integer cents —
docs.mx.com/api-reference/platform-api/reference/transactions. The
schema documentation for `amount` itself does not state a sign
convention explicitly ("The monetary amount of the `transaction`," no
positive/negative callout). Direction is instead carried by a
separate **`type`** enum: `CREDIT` or `DEBIT`, plus a boolean
**`is_expense`** flag — so MX's model is amount-magnitude +
type/flag, not signed-amount, matching the pattern the PRD hinted at
(`is_expense`/`type` flag instead of a signed float).

Account balances are the same `Decimal` dollar type: `balance` and
`available_balance` are documented as "usually... positive," with
asset accounts (`CHECKING`/`SAVINGS`/`INVESTMENT`) going negative on
overdraft and debt accounts (`CREDIT_CARD`/`LOAN`/`LINE_OF_CREDIT`/`MORTGAGE`)
going negative when overpaid —
docs.mx.com/api-reference/platform-api/reference/accounts.

## 10. Pending transactions

`status` field enum: **`POSTED`** / **`PENDING`** (documented as
nullable in some cases, and institutions sometimes only ever report
`POSTED`) — docs.mx.com/api-reference/platform-api/reference/transactions.
Linkage mechanism: MX *attempts* to preserve the same transaction
`guid` when a `PENDING` row transitions to `POSTED` ("a single
transaction may be updated from `PENDING` to `POSTED` and keep the
same `GUID`"), but this isn't guaranteed — when MX can't confidently
match the posted transaction to its pending predecessor, "the
`PENDING` transaction will often be deleted and replaced with a new
`POSTED` transaction (with a new `GUID`)" instead. As a hard failsafe,
independent of matching success, "all `PENDING` transactions are
deleted after 14 days." An integrator therefore cannot assume
guid-stability for pending→posted transitions and needs to handle both
the same-guid-update case and the delete-and-replace case (surfaced
via the Transactions webhook's `deleted`/`created` actions, §4).

## What I could not verify

- **Synchrony Bank / Lowe's Advantage / Amazon Store Card / NewRez /
  Shellpoint presence in MX's institution catalog** — the single
  most decision-relevant question for this research pass, and it's
  unanswered. `GET /institutions` requires an authenticated MX
  account; Pinch has none. This is the opposite situation from the
  sibling Plaid file, which had production credentials and could run
  `/institutions/search` + `/institutions/get_by_id` directly. **Next
  step if MX is pursued further: create the free MX dev account (100
  live users, per §7) and run this search before committing.**
- **The exact "16,000 institutions" figure** — shown above to be
  inconsistent in MX's own press materials (16,000 in 2021, 13,000 in
  2023); no docs.mx.com engineering page states a current total. Do
  not treat either number as current fact.
- **Whether the free/100-user tier includes Identity (Account Owner
  Identification)** — the products page describes the feature but
  no fetched page stated its billing tier the way it did for
  Holdings ("explicitly a separate paid product"). Confirming this
  would require a signed-in dashboard/pricing page check that wasn't
  reachable without an account.
- **Extended Transaction History's exact trigger mechanism** — "a
  premium aggregation-type job that won't start the throttle period"
  is the only description found; whether that's a distinct endpoint,
  a request parameter on the existing `aggregate` call, or something
  requested at member-creation time was not pinned down from the
  fetched pages.
- **Webhook signature verification** — searched for specifically
  (including guessing at an `X-MX-Signature`-style header name) and
  not found in any MX documentation page. This may genuinely not
  exist as a feature (MX instead offers transport-level auth on the
  receiving URL), but "not found in docs" isn't the same certainty as
  the sibling file's production-empirical findings — flagged rather
  than asserted as a hard fact.
- **Numeric API rate limits** beyond the per-member 3-hour
  aggregation cooldown — no general requests-per-minute/day ceiling
  was found in the fetched Platform API pages.
- **Whether `GET /institutions` is queryable from the free/sandbox
  tier without incurring cost or hitting a stricter limit** — not
  stated in the fetched pages.
- No PRODUCTION-EMPIRICAL section exists in this file at all, unlike
  the sibling Plaid research — everything above is a documentation
  claim, not a live-tested result, and should be treated with
  correspondingly less confidence until an actual MX account is
  provisioned and exercised.

## Addendum: integration-environment sandbox (verified 2026-08-04)

The free tier's developer environment includes a true fake-data sandbox:
the **MX Bank** test institution (`mxbank`), integration environment
only. Connecting it generates test accounts and transactions; it is
exempt from the aggregation throttle window. Username `mxuser`; any
password connects (`CONNECTED`), while scripted passwords drive every
other member status: `challenge`/`options`/`image` → `CHALLENGED` (the
three MFA types), `UNAUTHORIZED`/`INVALID`/`DISABLED` → `DENIED`,
`LOCKED` → `LOCKED`, `BAD_REQUEST`/`SERVER_ERROR`/`UNAVAILABLE` →
`FAILED`. A smaller **MXCU** test credit union also exists.
— docs.mx.com/resources/test-platform/mxbank/,
docs.mx.com/resources/test-platform/, docs.mx.com/resources/test-platform/mxcu/
