# Plaid Investments API — fact sheet

Sourced ONLY from `plaid.com/docs` (official Plaid documentation), pulled 2026-08-03. No third-party blogs, aggregators, or training-data recollection were used as sources of truth — every fact below is cited inline with the exact `plaid.com/docs` URL it was verified against. Where a fact is not stated in the official docs, this is called out explicitly as "not confirmed in official docs" rather than guessed.

This is grounding material for a future Investments PRD. Context: Pinch already integrates Plaid for Transactions/Accounts/Liabilities but has never requested the Investments product; investment accounts currently get balances only, never transaction history, by design (see team memory `plaid-investments-product-gap`).

Note on method: Plaid's docs site serves a machine-readable Markdown mirror of every page at the same path with `index.html.md` appended (e.g. `https://plaid.com/docs/api/products/investments/index.html.md`). That mirror was fetched directly to get complete, untruncated schema text (the rendered HTML page hides deeply-nested schema rows behind "expand" affordances that a text-extraction pass drops), and cross-checked against the rendered page where noted.

---

## 1. `/investments/holdings/get` — request/response shape

Source: [plaid.com/docs/api/products/investments/#investmentsholdingsget](https://plaid.com/docs/api/products/investments/index.html.md#investmentsholdingsget)

**Request fields:** `client_id`, `secret`, `access_token` (required), and an optional `options` object with an `account_ids` array to scope the accounts returned.

**Response** has five top-level fields: `accounts`, `holdings`, `securities`, `item`, `request_id`, plus a boolean `is_investments_fallback_item` ("When true, this field indicates that the Item's portfolio was manually created with the Investments Fallback flow.").

**`accounts` array** — same account object used across Plaid (`/investments/holdings/get` and `/investments/transactions/get` return the identical shape): `account_id`, `balances` (`available`, `current`, `limit`, `iso_currency_code`, `unofficial_currency_code`, `unofficial_currency_code`, and for margin: `margin_loan_amount` — described as "The total amount of borrowed funds in the account... For investment-type accounts, the margin balance is the total value of borrowed assets in the account"), `mask`, `name`, `official_name`, `type` (`investment`, `credit`, `depository`, `loan`, `brokerage` [legacy pre-2018-05-22], `other`), `subtype` (e.g. `ira`, `401k`, `money market`, `crypto exchange`, etc. — full type/subtype table at [plaid.com/docs/api/accounts/#account-type-schema](https://plaid.com/docs/api/accounts/index.html.md#account-type-schema)), plus Auth-only `verification_status`/`verification_name` fields that only populate for micro-deposit/database verification flows (not relevant to investment accounts).

**`holdings` array fields** (per the docs, holdings "represents a user's ownership of a security"): `account_id`, `security_id`, `institution_price`, `institution_price_as_of`, `institution_price_datetime` (nullable, more precise timestamp variant of `institution_price_as_of`), `institution_value`, `cost_basis`, `quantity`, `iso_currency_code`, `unofficial_currency_code`, `vested_quantity`, `vested_value` (both nullable — used for stock-compensation-type holdings), and `tax_lots` (array, can be empty; each lot has `institution_lot_id`, `original_purchase_datetime`, `quantity`, `purchase_price`, `cost_basis`, `current_value`, `position_type` e.g. `LONG`).

**`securities` array fields:** `security_id`, `isin`, `cusip`, `sedol`, `institution_id`, `institution_security_id`, `proxy_security_id`, `name`, `ticker_symbol`, `is_cash_equivalent` (boolean), `type` (e.g. `derivative`, `mutual fund`, `equity`, `cash`, `etf`, `fixed income`, `cryptocurrency`), `subtype` ("the security subtype of the holding... In rare instances, a null value is returned when institutional data is insufficient to determine the security subtype"), `close_price`, `close_price_as_of`, `unofficial_currency_code`, `update_datetime`, `market_identifier_code` (e.g. `XNAS`), `sector`, `industry`, `cfi_code`, `figi`, `option_contract` (object: `contract_type`, `expiration_date`, `strike_price`, `underlying_security_ticker` — null for non-options), `fixed_income` (object: `face_value`, `issue_date`, `maturity_date`, `yield_rate` {`percentage`, `type`} — null for non-fixed-income).

**`item` object** in the response includes `available_products`, `billed_products`, `consent_expiration_time`, `error`, `institution_id`, `institution_name`, `item_id`, `update_type`, `webhook`, `auth_method`.

Full annotated JSON example is on the page (accounts include a depository "Plaid Money Market" account alongside investment-type "Plaid IRA", "Plaid 401k", and "Plaid Crypto Exchange Account" accounts, confirming `/investments/holdings/get` returns holdings for `investment`-type accounts specifically while `accounts` can include non-investment accounts on the same Item).

---

## 2. `/investments/transactions/get` — request/response shape, pagination, lookback, sync/refresh

Source: [plaid.com/docs/api/products/investments/#investmentstransactionsget](https://plaid.com/docs/api/products/investments/index.html.md#investmentstransactionsget) and [plaid.com/docs/investments/#investments-transactions](https://plaid.com/docs/investments/index.html.md)

**Request fields:** `client_id`, `secret`, `access_token` (required); `start_date`, `end_date` (both required, `YYYY-MM-DD`); optional `options` object with `account_ids` (array), `count` (integer, default `100`, min `1`, max `500`), `offset` (integer, default `0`, min `0`), and `async_update` (boolean, default `false`, see below).

**Pagination:** count/offset based, not cursor-based. Response includes `total_investment_transactions`: "The total number of transactions available within the date range specified. If `total_investment_transactions` is larger than the size of the `transactions` array, more transactions are available and can be fetched via manipulating the `offset` parameter." Transactions are returned "in reverse-chronological order, and the sequence of transaction ordering is stable and will not shift."

**Response transaction object fields:** `investment_transaction_id`, `account_id`, `security_id`, `date`, `transaction_datetime` (nullable, more precise), `name`, `quantity`, `amount`, `price`, `fees`, `type`, `subtype`, `iso_currency_code`, `unofficial_currency_code`, `cancel_transaction_id` (nullable — links a `cancel`-type transaction to the transaction it cancels).

**Historical lookback — exactly 24 months, stated as a flat capability, no institution-dependence caveat in the docs:** "The `/investments/transactions/get` endpoint allows developers to retrieve up to 24 months of user-authorized transaction data for investment accounts." ([plaid.com/docs/api/products/investments/](https://plaid.com/docs/api/products/investments/index.html.md#investmentstransactionsget)) — restated identically at [plaid.com/docs/investments/#transactions](https://plaid.com/docs/investments/index.html.md#transactions): "provides up to 24 months of investment transactions data." The phrase "up to" is Plaid's own qualifier; the docs do not explicitly say whether shortfalls below 24 months are institution-dependent — **not confirmed in official docs whether it is a hard platform ceiling vs. institution-limited in practice.**

**`/investments/transactions/sync` — does not exist.** No such endpoint appears anywhere in the Investments API reference or the Investments product docs; the endpoint table on [plaid.com/docs/api/products/investments/](https://plaid.com/docs/api/products/investments/index.html.md) lists only `/investments/holdings/get`, `/investments/transactions/get`, and `/investments/refresh`. `/investments/transactions/get` is the only retrieval mechanism for investment transactions.

**`/investments/refresh` — exists, is an on-demand refresh endpoint.** Exact docs text (source: [plaid.com/docs/api/products/investments/#investmentsrefresh](https://plaid.com/docs/api/products/investments/index.html.md#investmentsrefresh)):

> "`/investments/refresh` is an optional endpoint for users of the Investments product. It initiates an on-demand extraction to fetch the newest investment holdings and transactions for an Item. This on-demand extraction takes place in addition to the periodic extractions that automatically occur one or more times per day for any Investments-enabled Item. If changes to investments are discovered after calling `/investments/refresh`, Plaid will fire webhooks: `HOLDINGS: DEFAULT_UPDATE` if any new holdings are detected, and `INVESTMENTS_TRANSACTIONS: DEFAULT_UPDATE` if any new investment transactions are detected. This webhook will typically not fire in the Sandbox environment, due to the lack of dynamic investment transactions and holdings data. To test this webhook in Sandbox, call `/sandbox/item/fire_webhook`. Updated holdings and investment transactions can be fetched by calling `/investments/holdings/get` and `/investments/transactions/get`. Note that the `/investments/refresh` endpoint is not supported by all institutions. If called on an Item from an institution that does not support this functionality, it will return a `PRODUCT_NOT_SUPPORTED` error."

It is synchronous from the caller's perspective ("triggers a synchronous request for fresh data, latency may be higher than for other Plaid endpoints (typically less than 10 seconds, but occasionally up to 30 seconds or more)"), request/response is minimal (`access_token` in, `request_id` out — no data is returned directly; you must re-call `/investments/holdings/get` / `/investments/transactions/get` afterward). It requires product-access approval: "`/investments/refresh` is offered as an add-on to Investments and has a separate fee model... To request access to this endpoint, submit a product access request or contact your Plaid account manager." This differs from the automatic webhook-driven background refresh, which Plaid runs on its own daily/overnight cadence with no caller action required (see §3).

---

## 3. Webhooks — `HOLDINGS: DEFAULT_UPDATE`, `INVESTMENTS_TRANSACTIONS: DEFAULT_UPDATE`/`HISTORICAL_UPDATE`, refresh cadence

Source: [plaid.com/docs/api/products/investments/#webhooks](https://plaid.com/docs/api/products/investments/index.html.md) and [plaid.com/docs/investments/#investments-updates-and-webhooks](https://plaid.com/docs/investments/index.html.md#investments-updates-and-webhooks)

There are three investments-related webhooks, all sharing the same envelope fields (`webhook_type`, `webhook_code`, `item_id`, `user_id`, `error`, `environment`) plus type-specific counters:

- **`HOLDINGS: DEFAULT_UPDATE`** — "Fired when new or updated holdings have been detected on an investment account. The webhook typically fires in response to any newly added holdings or price changes to existing holdings, most commonly after market close." Payload adds `new_holdings` (number — "The number of new holdings reported since the last time this webhook was fired") and `updated_holdings` (number — same wording for updates).
- **`INVESTMENTS_TRANSACTIONS: DEFAULT_UPDATE`** — "Fired when new transactions have been detected on an investment account." Payload adds `new_investments_transactions` and `cancelled_investments_transactions` (both number, "since the last time this webhook was fired").
- **`INVESTMENTS_TRANSACTIONS: HISTORICAL_UPDATE`** — "Fired after an asynchronous extraction on an investment account" — only relevant when `/investments/transactions/get` was called with `async_update: true` on an Item not initialized with Investments at Link time (see §2, §8). Same payload shape as `DEFAULT_UPDATE` (`new_investments_transactions`, `cancelled_investments_transactions`).

**Documented cadence, quoted directly:**
- [plaid.com/docs/investments/#investments-updates-and-webhooks](https://plaid.com/docs/investments/index.html.md#investments-updates-and-webhooks): "Investments data is not static, since users' holdings will change as they trade and as market prices fluctuate. Plaid typically checks for updates to investment data overnight, after market hours."
- [plaid.com/docs/investments/add-to-app/#fetching-investment-data](https://plaid.com/docs/investments/add-to-app/index.html.md#fetching-investment-data): "Investments data is typically updated daily, after market close."
- The `HOLDINGS: DEFAULT_UPDATE` description itself says the webhook fires "most commonly after market close."

There is no cursor/sync webhook analog for Investments (contrast with Transactions' `SYNC_UPDATES_AVAILABLE`); the recommended pattern is: listen for `DEFAULT_UPDATE` webhooks, then re-call `/investments/holdings/get` / `/investments/transactions/get` with only the changed date range. Quoted: "When updating an Item with new Investments transactions data, it is recommended to call `/investments/transactions/get` with only the date range that needs to be updated, rather than the maximum available date range, in order to reduce the amount of data that you must receive and process."

---

## 4. Adding Investments to an existing Item — `additional_consented_products`, update mode, and the four `/link/token/create` product arrays

Sources: [plaid.com/docs/api/link/#linktokencreate](https://plaid.com/docs/api/link/index.html.md#linktokencreate), [plaid.com/docs/link/initializing-products/](https://plaid.com/docs/link/initializing-products/index.html.md), [plaid.com/docs/link/update-mode/](https://plaid.com/docs/link/update-mode/index.html.md)

**Yes** — `additional_consented_products` in Link update mode is exactly the documented mechanism for adding a product like Investments to an existing Item without a full re-link. From the update-mode doc: "When using update mode to add product consent, you must use the `additional_consented_products` parameter, not the `products` parameter... if Link was initialized with just Transactions and you want to add Signal, you would pass in `signal` to the `additional_consented_products` field." Also: "An Item's `access_token` does not change when using Link in update mode, so there is no need to repeat the exchange token process." Note the docs flag an exception class that does NOT use this path: "To add one of these products to an Item that did not previously have it enabled, you will need to send the user through update mode" using the standard `products` array instead for **Assets, Statements, Income, and Plaid Check Consumer Report** — Investments is not in that exception list, so the `additional_consented_products` path applies to it.

**Exact field definitions**, quoted verbatim from [plaid.com/docs/api/link/#linktokencreate](https://plaid.com/docs/api/link/index.html.md#linktokencreate):

- **`products`** (required if not in update mode): "List of Plaid product(s) that the linked Item must support. If launching Link in update mode, should be omitted (unless you are using update mode to add a credit product, such as Assets, Statements, Income, or Plaid Check Consumer Report, to an existing Item); at least one `product` is required otherwise." Also: "Only institutions that support _all_ requested products can be selected; if a user attempts to select an institution that does not support a listed product, a 'Connectivity not supported' error message will appear in Link."
- **`required_if_supported_products`**: "List of Plaid product(s) you wish to use only if the institution and account(s) selected by the user support the product. Institutions that do not support these products will still be shown in Link." (No overlap allowed with `products`, `optional_products`, or `additional_consented_products`.)
- **`optional_products`**: "List of Plaid product(s) that will enhance the consumer's use case, but that your app can function without. Plaid will attempt to fetch data for these products on a best-effort basis, and failure to support these products will not affect Item creation." (No overlap allowed with `products`, `required_if_supported_products`, or `additional_consented_products`.)
- **`additional_consented_products`**: "List of additional Plaid product(s) you wish to collect consent for to support your use case. These products will not be billed until you start using them by calling the relevant endpoints... Institutions that do not support these products will still be shown in Link." (No overlap allowed with `products` or `required_if_supported_products`.)

**Behavior at institution selection / Link init**, per the summary table at [plaid.com/docs/link/initializing-products/#initializing-products-during-link](https://plaid.com/docs/link/initializing-products/index.html.md#initializing-products-during-link):

| Array | Restricts institution list? | What happens if institution/account doesn't support it |
| --- | --- | --- |
| `products` | **Yes** — "Only institutions that support all products in this array will be available in Link." | Item creation fails outright; institution isn't even selectable. |
| `required_if_supported_products` | No | "If the institution or selected account doesn't support these products, they will be ignored and Item creation will succeed." |
| `optional_products` | No | "Plaid will attempt to get data for these products, but if this fails or the institution or selected account type doesn't support one or more of these products, Item creation will still succeed." (best-effort, does not fail the OAuth flow even if permission is withheld) |
| `additional_consented_products` | No | Consent is collected regardless of institution support; "Plaid will not extract data for these products and Item creation will not fail if the products are unavailable or incompatible." |

`optional_products` vs `required_if_supported_products`, further distinguished: Optional Products treats support as best-effort and will **not** fail the Link attempt even if the necessary OAuth permissions aren't granted (the product silently becomes unavailable until fixed via update mode later); Required if Supported Products, when the institution/account does support it, is treated exactly as if it had been in `products` — meaning if the institution uses OAuth and the user doesn't grant that permission, "the Link attempt will error and the user will be prompted to retry the OAuth flow."

`balance` is explicitly called out as never a valid value in any of these four arrays — Balance auto-initializes with any other product.

---

## 5. Billing for the Investments product

Source: [plaid.com/docs/account/billing/](https://plaid.com/docs/account/billing/index.html.md) and [plaid.com/docs/investments/#investments-pricing](https://plaid.com/docs/investments/index.html.md#investments-pricing)

**No dollar figures are published.** The billing page opens with: "A price list is not available in the documentation. To view pricing, apply for Production access. Pricing information for Pay-as-you-go and Growth plans will be displayed on the last page before you submit your request. For Custom plans, select the Custom option and submit the form, and sales will reach out to discuss pricing." The Investments-specific pricing section confirms the model without a number: "Investments is billed on a subscription model; Investments Refresh is billed on a per-request flat fee model. To view the exact pricing you may be eligible for, apply for Production access or contact sales."

**What the docs do say about the model:**
- Investments (the main product) is subscription-fee-billed: "Under the subscription fee model, an Item will incur a monthly subscription fee as long as a valid `access_token` exists for the Item." Billing cycles are calendar-month, UTC, not pro-rated for mid-month creation/removal, and the only way to stop the charge is `/item/remove` (an Item, once given a subscription product, cannot have that subscription individually canceled while the Item stays alive).
- **Investments has two separate, independently-triggered subscriptions**, quoted directly: "Investments has two separate subscriptions that can be associated with an Item: Investments Holdings and Investments Transactions. Adding Investments to an Item via `/link/token/create` or by calling `/investments/holdings/get` adds the Investments Holdings subscription. Calling `/investments/transactions/get` on an Item adds both the Investments Transactions and Investments Holdings subscriptions." This means initializing Link with `investments` in `products` bills Investments Holdings immediately, but Investments Transactions is only billed the first time `/investments/transactions/get` is actually called — even though (per [plaid.com/docs/link/initializing-products/#impacts-of-product-initialization-on-billing](https://plaid.com/docs/link/initializing-products/index.html.md#impacts-of-product-initialization-on-billing)) "Plaid will still pre-fetch investment transaction history when you initialize with Investments, even though you are not yet being billed for Investments Transactions."
- **`/investments/refresh` is billed separately** as a per-request flat fee, explicitly carved out from the Investments subscription: "Products that use subscription fee pricing are: ... Investments (except for the `/investments/refresh` endpoint)"; refresh endpoints are listed under the "Per-request flat fee" model instead.
- Trial-plan note: "If you add a subscription-billed product to an Item during your trial (for example, by creating an Item initialized with Transactions, Investments, or Liabilities), upon upgrading to a paid plan, you will begin to be charged for that subscription."

---

## 6. Amount sign convention — Investments transactions vs. regular Transactions

Source: [plaid.com/docs/api/products/investments/](https://plaid.com/docs/api/products/investments/index.html.md#investmentstransactionsget), [plaid.com/docs/api/accounts/#investment-transaction-types-schema](https://plaid.com/docs/api/accounts/index.html.md#investment-transaction-types-schema), [plaid.com/docs/investments/#transactions](https://plaid.com/docs/investments/index.html.md#transactions), [plaid.com/docs/api/products/transactions/](https://plaid.com/docs/api/products/transactions/index.html.md#transactionsget)

**Investments (`/investments/transactions/get`), exact docs wording for the `amount` field:** "Positive values when cash is debited, e.g. purchases of stock; negative values when cash is credited, e.g. sales of stock. ... Treatment remains the same for cash-only movements unassociated with securities." The Investment transaction types schema page states the same rule generally, not per-subtype: "Note that transactions representing inflow of cash will appear as negative amounts, outflow of cash will appear as positive amounts." The Investments intro page restates it in plain language: "Inflow, such as stock sales, is shown as a negative amount, and outflow, such as stock purchases, is positive." **This is one uniform sign rule applied across all `type`/`subtype` values** (buy, sell, cash, fee, transfer) — the docs do not document any per-subtype exception; a `sell` (cash inflow) is negative, a `buy` (cash outflow) is positive, a `dividend` (`cash`/`dividend`, cash inflow) is negative, and a `fee` (cash outflow) is positive. This is corroborated by the worked JSON example on the page: a `dividend` transaction shows `"amount": -8.72`, a `sell` transaction shows `"amount": -1289.01`, and a `buy` transaction shows `"amount": 7.7`.

**Regular Transactions (`/transactions/get`), exact docs wording for contrast:** "Positive values when money moves out of the account; negative values when money moves in. For example, debit card purchases are positive; credit card payments, direct deposits, and refunds are negative."

**The directional convention is actually the same shape** (positive = outflow/debit, negative = inflow/credit) for both products — the practical difference for a PRD is just which events count as "out" vs "in": for regular Transactions it's plain spending vs. deposits; for Investments it's specifically cash flow associated with securities activity (buying a security is an outflow/positive even though no "cash left the account" in a checking-account sense — it moved from cash to a position within the same account).

---

## 7. `/accounts/get` / `/accounts/balance/get` — investment balances without Investments consent

Sources: [plaid.com/docs/api/accounts/#accountsget](https://plaid.com/docs/api/accounts/index.html.md#accountsget), [plaid.com/docs/api/products/signal/#accountsbalanceget](https://plaid.com/docs/api/products/signal/index.html.md#accountsbalanceget)

**Confirmed: yes, balance data for investment-type accounts is independent of Investments product consent.**

- `/accounts/get`, exact docs wording: "`/accounts/get` is free to use and retrieves cached information, rather than extracting fresh information from the institution. The balance returned will reflect the balance at the time of the last successful Item update. If the Item is enabled for a regularly updating product, such as Transactions, Investments, or Liabilities, the balance will typically update about once a day, as long as the Item is healthy. If the Item is enabled only for products that do not frequently update, such as Auth or Identity, balance data may be much older." Nothing in the docs gates this endpoint, or the `balances` object it returns for `investment`-type accounts, behind Investments-product consent — it works on any Item, for any account type, using whatever product update cadence happens to be active.
- `/accounts/balance/get`, exact docs wording: "This endpoint can be used for existing Items that were added via any of Plaid's other products. This endpoint can be used as long as Link has been initialized with any other product; `balance` itself is not a product that can be used to initialize Link." This explicitly confirms balance retrieval is **not** gated behind Investments (or any single specific product) — it rides on top of whatever product the Item was actually initialized with (Auth, Transactions, Investments, Liabilities, etc.), and Balance itself auto-initializes.

So Pinch's current behavior — investment accounts return balances via cached `/accounts/get` calls, without ever consenting to Investments — is exactly what the docs describe as expected: Balance/Auth-tier balance data is orthogonal to Investments consent. What Investments product consent additionally unlocks is `holdings` (security-level positions) and `investment_transactions` (buy/sell/dividend/fee history) — neither of which `/accounts/get` or `/accounts/balance/get` ever return.

---

## 8. `PRODUCT_NOT_READY` for Investments endpoints

Source: [plaid.com/docs/errors/item/#product_not_ready](https://plaid.com/docs/errors/item/index.html.md#product_not_ready)

**Definition (applies across products):** "Returned when a data request has been made for a product that is not yet ready."

**Investments-specific cause, quoted directly:** "`/investments/transactions/get` was called before the investments data could be extracted. This typically happens when the endpoint is called with the option for `async_update` set to true, and called again within a few seconds of linking the Item. It will also happen if `/investments/transactions/get` (with `async_update` set to true) was called for the first time on an Item that was not initialized with `investments` in the `/link/token/create` call."

Cross-referenced against [plaid.com/docs/api/products/investments/#investmentstransactionsget](https://plaid.com/docs/api/products/investments/index.html.md#investmentstransactionsget): the `async_update` request option is described as: "If the Item was not initialized with the investments product via the `products`, `required_if_supported_products`, or `optional_products` array when calling `/link/token/create`, and `async_update` is set to true, the initial Investments extraction will happen asynchronously. Plaid will subsequently fire a `HISTORICAL_UPDATE` webhook when the extraction completes. When `false`, Plaid will wait to return a response until extraction completion and no `HISTORICAL_UPDATE` webhook will fire. Note that while the extraction is happening asynchronously, calls to `/investments/transactions/get` and `/investments/refresh` will return `PRODUCT_NOT_READY` errors until the extraction completes." Separately (also confirmed at [plaid.com/docs/investments/#investments-transactions-initialization-behavior](https://plaid.com/docs/investments/index.html.md#investments-transactions-initialization-behavior)): "If no investments history is ready when `/investments/transactions/get` is called, it will return a `PRODUCT_NOT_READY` error" — this is the synchronous (default, `async_update: false`) path's failure mode when called too early on an Item that already has Investments in `products`.

**Recommended client handling — it differs by which code path you're on:**
1. **Default/synchronous path (`async_update` unset or `false`, Investments in `products`/`required_if_supported_products`/`optional_products` at Link time):** Do not poll/retry manually — per [plaid.com/docs/investments/#investments-transactions-initialization-behavior](https://plaid.com/docs/investments/index.html.md#investments-transactions-initialization-behavior), "by default, Investments Transactions operates synchronously and will not fire a webhook to indicate when initial data is ready for an Item. If investments transactions data is not ready when `/investments/transactions/get` is first called, Plaid will wait for the data. For this reason, calling `/investments/transactions/get` immediately after Link may take up to one to two minutes to return." I.e. the endpoint call itself blocks/waits rather than erroring in the normal case; `PRODUCT_NOT_READY` here is the edge case rather than the expected flow.
2. **Async path (`async_update: true`, used specifically when adding Investments to an Item post-Link without listing it in `/link/token/create`):** the raw error's own `error_message` string is: "the requested product is not yet ready. please provide a webhook or try the request again later" — and the documented pattern is to wait for the `INVESTMENTS_TRANSACTIONS: HISTORICAL_UPDATE` webhook rather than tight-poll, per the `async_update` field docs above ("Plaid will subsequently fire a `HISTORICAL_UPDATE` webhook when the extraction completes").

The general troubleshooting guidance on the error page (shared across all `PRODUCT_NOT_READY` occurrences, not Investments-specific): listen for the applicable product-ready webhook where one exists, or retry later — Plaid does not document a specific recommended backoff schedule for Investments in this error page.

---

## 9. Sandbox support for Investments

Sources: [plaid.com/docs/investments/#testing-investments](https://plaid.com/docs/investments/index.html.md#testing-investments), [plaid.com/docs/sandbox/user-custom/](https://plaid.com/docs/sandbox/user-custom/index.html.md), [plaid.com/docs/sandbox/test-credentials/](https://plaid.com/docs/sandbox/test-credentials/index.html.md), [plaid.com/docs/api/sandbox/#sandboxitemfire_webhook](https://plaid.com/docs/api/sandbox/index.html.md#sandboxitemfire_webhook)

**Basic support:** "Investments can be tested in Sandbox without any additional permissions." ([plaid.com/docs/investments/#testing-investments](https://plaid.com/docs/investments/index.html.md#testing-investments))

**`user_good` / `pass_good`:** Per [plaid.com/docs/sandbox/test-credentials/](https://plaid.com/docs/sandbox/test-credentials/index.html.md), `user_good`/`pass_good` provide "basic account access to most Plaid products" generically — the docs do not describe `user_good` producing any specific investment holdings/transactions dataset; **not confirmed in official docs** exactly what investment data (if any) `user_good` surfaces beyond the generic statement that it works across products. For realistic/rich investments data, the docs point specifically to the **custom Sandbox user** flow instead:

> "To test with realistic data, use the custom user. If provided real-world ticker symbols, Plaid will automatically populate securities with realistic data for both options and fixed income. For examples, see the sample Investments custom user." ([plaid.com/docs/investments/#testing-investments](https://plaid.com/docs/investments/index.html.md#testing-investments), linking to a Plaid-maintained example at `github.com/plaid/sandbox-custom-users/blob/main/investments/brokerage_custom_user.json` — that GitHub file itself is outside the plaid.com/docs scope of this fact sheet, so its exact contents are not verified here.)

**Important custom-user gotcha, quoted directly:** "When using the custom Sandbox user, Investments must be placed in the `products` array of `/link/token/create` and cannot be used in the `optional_products`, `additional_consented_products`, or `required_if_supported_products` array. Omitting `investments` from the `products` array may cause custom Sandbox investments data not to be loaded." This is a real constraint for any future Pinch Sandbox test — the `optional_products`/`additional_consented_products` patterns discussed in §4 (which are the recommended production-safe way to add Investments) will **not** load custom investments fixture data in Sandbox; `products` must be used for test-data purposes even if production integration ultimately uses a softer array.

**Custom account/security schema:** [plaid.com/docs/sandbox/user-custom/](https://plaid.com/docs/sandbox/user-custom/index.html.md) documents the configuration object for custom users, which supports `investment` as an account `type` value, plus an `investment_transactions` override array with a `type` enum matching production (`buy`, `sell`, `cash`, `fee`, `transfer` — descriptions identical to §2/§6's production enum) and a `securities` config keyed by `ticker_symbol`/`cusip`/`isin`.

**Default (non-custom) Sandbox institutions supporting investment accounts:** the general Sandbox docs state "All of the institutions that are available in the Plaid Production environment are also available in Sandbox," plus Sandbox-only institutions for integration testing (e.g. Houndstooth Bank, `ins_109512`, used to test fallback flows) — but **the specific list of which default (canned, non-custom) Sandbox test institutions expose investment-type accounts by default is not itemized as such anywhere in [plaid.com/docs/sandbox/institutions/](https://plaid.com/docs/sandbox/institutions/index.html.md); not confirmed in official docs.** Plaid's own steer for anyone wanting real investments fixture data is the custom-user path above, not a specific default institution.

**Sandbox-only helper endpoints relevant to Investments testing:**
- **`/sandbox/item/fire_webhook`** — confirmed to support Investments: "Valid Sandbox `DEFAULT_UPDATE` webhook types include: `AUTH`, `IDENTITY`, `TRANSACTIONS`, `INVESTMENTS_TRANSACTIONS`, `LIABILITIES`, `HOLDINGS`. If the Item does not support the product, a `SANDBOX_PRODUCT_NOT_ENABLED` error will result." ([plaid.com/docs/api/sandbox/#sandboxitemfire_webhook](https://plaid.com/docs/api/sandbox/index.html.md#sandboxitemfire_webhook)) This is also explicitly the documented way to test `/investments/refresh`'s webhook-firing behavior, since that webhook "will typically not fire in the Sandbox environment, due to the lack of dynamic investment transactions and holdings data" otherwise (§2).
- **`/sandbox/public_token/create`** — general-purpose Sandbox Item-creation-via-API endpoint (bypasses the Link UI), usable with `options.override_username`/`options.override_password` to attach a custom user's investments fixture data to a generated `public_token`, per [plaid.com/docs/sandbox/user-custom/](https://plaid.com/docs/sandbox/user-custom/index.html.md).
- No Investments-specific sandbox config endpoint beyond the general custom-user mechanism was found in the docs; `/sandbox/item/set_verification_status` (mentioned in the research brief) is an Auth micro-deposit-verification helper, not Investments-related — not applicable here.

---

## Appendix — full `type`/`subtype` enum for investment transactions

Source: [plaid.com/docs/api/accounts/#investment-transaction-types-schema](https://plaid.com/docs/api/accounts/index.html.md#investment-transaction-types-schema). This table was reconstructed from the page's rendered HTML (the plain-Markdown mirror silently drops the actual enum key strings and keeps only descriptions, so this list was extracted from `<code class="SchemaRow_attributeKey...">` anchors in the live rendered page to guarantee no subtype was missed or mislabeled). Types with no subtypes listed (`cancel`) have none in the schema.

**`buy`** — "Buying an investment"
- `assignment` — Assignment of short option holding
- `contribution` — Inflow of assets into a tax-advantaged account
- `buy` — Purchase to open or increase a position
- `buy to cover` — Purchase to close a short position
- `dividend reinvestment` — Purchase using proceeds from a cash dividend
- `interest reinvestment` — Purchase using proceeds from a cash interest payment
- `long-term capital gain reinvestment` — Purchase using long-term capital gain cash proceeds
- `short-term capital gain reinvestment` — Purchase using short-term capital gain cash proceeds

**`sell`** — "Selling an investment"
- `distribution` — Outflow of assets from a tax-advantaged account
- `exercise` — Exercise of an option or warrant contract
- `sell` — Sell to close or decrease an existing holding
- `sell short` — Sell to open a short position

**`cancel`** — "A cancellation of a pending transaction" (no subtypes)

**`cash`** — "Activity that modifies a cash position"
- `account fee` — Fees paid for account maintenance
- `contribution` — Inflow of assets into a tax-advantaged account
- `deposit` — Inflow of cash into an account
- `dividend` — Inflow of cash from a dividend
- `stock distribution` — Inflow of stock from a distribution
- `interest` — Inflow of cash from interest
- `legal fee` — Fees paid for legal charges or services
- `long-term capital gain` — Long-term capital gain received as cash
- `management fee` — Fees paid for investment management of a mutual fund or other pooled investment vehicle
- `margin expense` — Fees paid for maintaining margin debt
- `non-qualified dividend` — Inflow of cash from a non-qualified dividend
- `non-resident tax` — Taxes paid on behalf of the investor for non-residency in investment jurisdiction
- `pending credit` — Pending inflow of cash
- `pending debit` — Pending outflow of cash
- `qualified dividend` — Inflow of cash from a qualified dividend
- `short-term capital gain` — Short-term capital gain received as cash
- `tax` — Taxes paid on behalf of the investor
- `tax withheld` — Taxes withheld on behalf of the customer
- `transfer fee` — Fees incurred for transfer of a holding or account
- `trust fee` — Fees related to administration of a trust account
- `unqualified gain` — Unqualified capital gain received as cash
- `withdrawal` — Outflow of cash from an account

**`fee`** — "Fees on the account, e.g. commission, bookkeeping, options-related."
- `account fee` — Fees paid for account maintenance
- `adjustment` — Increase or decrease in quantity of item
- `dividend` — Inflow of cash from a dividend
- `interest` — Inflow of cash from interest
- `interest receivable` — Inflow of cash from interest receivable
- `long-term capital gain` — Long-term capital gain received as cash
- `legal fee` — Fees paid for legal charges or services
- `management fee` — Fees paid for investment management of a mutual fund or other pooled investment vehicle
- `margin expense` — Fees paid for maintaining margin debt
- `non-qualified dividend` — Inflow of cash from a non-qualified dividend
- `non-resident tax` — Taxes paid on behalf of the investor for non-residency in investment jurisdiction
- `qualified dividend` — Inflow of cash from a qualified dividend
- `return of principal` — Repayment of loan principal
- `short-term capital gain` — Short-term capital gain received as cash
- `stock distribution` — Inflow of stock from a distribution
- `tax` — Taxes paid on behalf of the investor
- `tax withheld` — Taxes withheld on behalf of the customer
- `transfer fee` — Fees incurred for transfer of a holding or account
- `trust fee` — Fees related to administration of a trust account
- `unqualified gain` — Unqualified capital gain received as cash

**`transfer`** — "Activity that modifies a position, but not through buy/sell activity e.g. options exercise, portfolio transfer"
- `assignment` — Assignment of short option holding
- `adjustment` — Increase or decrease in quantity of item
- `exercise` — Exercise of an option or warrant contract
- `expire` — Expiration of an option or warrant contract
- `merger` — Stock exchanged at a pre-defined ratio as part of a merger between companies
- `request` — Request fiat or cryptocurrency to an address or email
- `send` — Inflow or outflow of fiat or cryptocurrency to an address or email
- `spin off` — Inflow of stock from spin-off transaction of an existing holding
- `split` — Inflow of stock from a forward split of an existing holding
- `trade` — Trade of one cryptocurrency for another
- `transfer` — Movement of assets into or out of an account

Note: several subtype strings (e.g. `dividend`, `tax`, `account fee`) legitimately repeat across different parent `type` values with the same or near-identical descriptions (e.g. `dividend` appears under both `cash` and `fee`) — this is a documented deliberate overlap in Plaid's schema, not a transcription error.
