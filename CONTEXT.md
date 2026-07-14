# Pinch — Ubiquitous Language

Glossary for the Pinch domain. Terms here are canonical: use them in code,
docs, and conversation. Implementation details do not belong in this file.

## Product identity

- **Pinch** — the product: an AI-native personal finance tracker (net worth,
  transactions, loans, assets). The brand users see.
- **Penny** — the AI assistant inside Pinch. Penny is a feature of Pinch, not
  the product itself. Users chat with Penny; Penny tags transactions, imports
  CSVs, and answers questions about the user's finances.
- **Pinch CLI** — the public command-line tool (`pinch`). It interacts with a
  Pinch server exclusively through the public developer API; it has no
  privileged access. Internal developer tooling is not part of the Pinch CLI.
- **Developer API** — the public HTTP API through which users (and the Pinch
  CLI) access their own data. If something can't be done via the Developer
  API, it isn't automatable — parity with the app is the goal.

## Importing

- **Import** — a batch created by one file upload into a manual account, with
  a lifecycle: uploaded → mapped → previewed → committed. An import is
  undoable as a unit. Rows must validate (parseable date, money amount)
  before commit; nothing touches the ledger until commit.
- **Import profile** — a saved, user-confirmed column mapping for a given
  file shape (delimiter, date format, amount sign convention, column roles).
  The first mapping for an unrecognized file shape arrives as a **suggested
  mapping** the user confirms or corrects (suggestion quality is an
  implementation concern; Penny assumes the job when she lands); subsequent
  files matching the profile map deterministically with no AI involved.
- **Auto-file** — an import-commit option for historical backfill: the
  classification pipeline runs normally, but its proposals are applied and
  marked reviewed immediately instead of entering the inbox. Auto-filed
  decisions are recorded in the correction log as the system's, never the
  user's — they are not rule-promotion evidence and not eval data.
- **Duplicate flag** — rows whose fingerprint (account, date, amount,
  normalized description) matches an existing transaction — or another row
  in the same file — are flagged in the preview and skipped by default; the
  user may override per row. Distinct real-world transactions can collide
  (two identical coffees, same day); the per-row override is the escape
  hatch, which is why skipping is a default and never silent.

## Classification

- **Category** — the canonical classification of a transaction, drawn from
  the user's editable taxonomy. A transaction has at most one category —
  never more than one, so double-counting is impossible — and may be
  **uncategorized**: the classification pipeline's bottom case and a
  legitimate reviewed state, never an error. Categories are the basis for
  budgets and reporting; uncategorized transactions report as their own
  bucket. Assigned automatically (rules, then history, then AI) and
  confirmed or corrected by the user.
- **Category hierarchy** — categories may nest (e.g. Food → Restaurants). A
  transaction is assigned to exactly one node; membership in ancestor
  categories is derived at reporting time, never stored. A transaction
  categorized `Restaurants` counts toward `Food` by inheritance.
- **Tag** — a free-form, optional label; a transaction may carry many. Tags
  exist for user searches and ad-hoc grouping (e.g. `vacation-2026`,
  `reimbursable`). Tags are never the basis for budgets.
- **Proposal** — the category (and tags) suggested for an incoming
  transaction before the user has reviewed it. Every incoming transaction —
  even one matched by a user rule — carries a proposal, never an accepted
  category. A proposal may be **empty**: every stage of the pipeline
  abstained, and the suggestion is "no category". Each proposal records its
  **provenance**: rule, history, AI, or none.
- **Payee** — the normalized form of a transaction's raw description: the
  deterministic key that rule conditions and history matching operate on,
  ledger-wide. Exact by design — recognizing "the same merchant, written
  differently" is the AI stage's job, never the deterministic pipeline's.
- **Provenance** — how a proposal was produced: *rule* (a user rule matched),
  *history* (same payee previously confirmed), *AI* (Penny classified it),
  or *none* (the pipeline ran and every stage abstained). Always shown to
  the user during review.
- **Review** — the act of accepting or correcting proposals on incoming
  transactions. All incoming transactions require review. The dashboard
  presents them grouped by day; the user may accept transactions
  individually or accept a whole day at once. Corrections feed back into the
  classification system (the flywheel). A reviewed transaction returns to
  the inbox if its source data changes materially (e.g. amount), but not
  cosmetically (e.g. description).
- **Correction log** — the append-only record of every review decision:
  what was proposed, with what provenance, and what was accepted or
  corrected — self-contained, surviving the deletion of anything it
  mentions. Every decision carries its **actor**: the user, or the system
  (an auto-filed import applying the user's own precedent). Append-only
  includes retraction: when the data a decision was made against is undone
  (e.g. an import reverted), the decision is voided by a later entry, never
  deleted; a changed mind is a later entry, never an edit. It is
  simultaneously the flywheel's memory (few-shot context, rule-promotion
  evidence — user decisions only) and the eval dataset for improving the
  categorization prompt on ever-cheaper models.
- **Rule** — a user-defined condition → action pair applied deterministically
  to incoming transactions (e.g. payee contains "COSTCO" → propose category
  Groceries). Rules take precedence over history and AI. Pinch may propose
  a new rule when the user's own filings repeat consistently (**rule
  promotion**); a proposed rule is never law — a rule is only ever created
  with user consent.

## Tenancy

- **Ledger** — the unit of data ownership and sharing. All financial data
  (accounts, transactions, categories, rules, imports) belongs to a ledger,
  never directly to a user. A user may belong to multiple ledgers (e.g. a
  household and an LLC). In v0 every user gets exactly one auto-created
  ledger and sharing is not exposed; ledger sharing (members, roles,
  per-account visibility) is a committed post-v0 feature.

## Accounts

- **Account** — anything that holds value and contributes to net worth. Every
  account has a *kind*: depository, credit, investment, loan, or asset. Loans
  and credit carry negative balances. All kinds share one concept — there is
  no separate "asset tracker" or "loan tracker" entity.
- **Connection** — a live link to an external data source (e.g. one Plaid
  Item = one institution login). A connection yields one or more accounts and
  owns credentials and sync state. Manual accounts have no connection.
- **Manual account** — an account maintained by the user without a
  connection: balances entered by hand, transactions entered manually or via
  file import.
- **Balance entry** — one observed balance for an account at a point in
  time, hand-entered by the user (providers supply them too, later). An
  account's current balance is its latest entry; transactions are records
  of money movement, never balance arithmetic — reconciling the two is a
  deliberate future design, not an omission.
- **Balance history** — the per-account time series of balance entries that
  powers net worth over time.
- **Valuation provider** — an external source of value estimates for asset
  accounts (e.g. Zillow for a home), analogous to Plaid for bank accounts.
- **Net worth** — the sum of all account balances at a point in time. A
  derived quantity, not a separate system.
- **Holding** — a position in an investment account: a security, a quantity,
  a market price/value (cost basis where available). Investment accounts
  support holdings early on; depth (lots, performance analytics) grows over
  time.
- **Loan terms** — the contractual parameters of a loan account: APR,
  minimum/expected payment, origination date and amount, maturity. Sourced
  from Plaid liabilities when available, otherwise supplied by the user
  (Penny assists).
- **Payoff projection** — the projected payoff date and total interest for a
  loan, computed by simulating amortization forward under (a) the user's
  observed payment behavior (from transfer history into the loan) and (b) the
  contractual minimum. The difference between the two is the headline number.

## Money

- **Amount** — every money value is an integer count of minor units plus an
  ISO 4217 currency code. Never floats; never a bare number without currency.
- **Primary currency** — the single currency a user's reports and totals are
  expressed in, chosen at signup. Foreign-currency transactions are stored
  faithfully in their native currency; v0 reports them at a current-rate
  approximation. Historical-rate valuation is out of scope for now.

## Transactions

- **Transaction** — a single money movement on an account, sourced from a
  provider sync (e.g. Plaid), a file import, or manual entry. Its date is
  the institution's calendar date (never a localized timestamp); its amount
  is signed from the account's perspective — negative is money out.
- **Source data** — the fields of a transaction owned by its origin (raw
  description, amount, date, pending status, provider identifiers). Syncs and
  re-imports may rewrite source data; users cannot.
- **User data** — the fields owned by the user (category, tags, display name,
  notes, reviewed status). Syncs may never alter user data. When a pending
  transaction is replaced by its posted form, the replacement inherits the
  predecessor's user data.
- **Pending / Posted** — a transaction's settlement state at the institution.
  Pending transactions are ingested and shown from day one; posting replaces
  the pending record (with user-data inheritance) rather than duplicating it.
- **Split line** — a division of a transaction into parts, each with its own
  amount and category, summing exactly to the transaction amount. The
  "exactly one category" rule formally applies to the split line; an unsplit
  transaction is the degenerate single-line case. Reporting operates on
  lines. (Roadmap: Penny proposes splits from an uploaded receipt.)
- **Reimbursement** — money returned to the user offsetting an earlier
  expense. Not yet first-class: the convention is to categorize the incoming
  credit to the same category as the original expense, netting it out.
- **Transfer** — a link between exactly two transactions of the same user
  (opposite signs, matching amounts) marking the pair as money movement
  between accounts, not income or expense. Reports exclude transfers by
  default. A transaction may also be marked as a transfer whose counterparty
  is untracked (the other account isn't in Pinch); it is still excluded from
  spending.
