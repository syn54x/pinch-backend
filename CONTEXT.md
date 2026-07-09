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
  Penny infers the first mapping; subsequent files matching the profile map
  deterministically with no AI involved.
- **Duplicate flag** — at commit time, rows whose fingerprint (account, date,
  amount, normalized description) matches an existing transaction are flagged
  in the preview and skipped by default; the user may override per row.

## Classification

- **Category** — the canonical classification of a transaction. Every
  transaction has exactly one category, drawn from the user's editable
  taxonomy. Categories are the basis for budgets and reporting; no
  double-counting is possible. Assigned automatically (rules, then AI) and
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
  category. Each proposal records its **provenance**: rule, history, or AI.
- **Provenance** — how a proposal was produced: *rule* (a user rule matched),
  *history* (same payee previously confirmed), or *AI* (Penny classified it).
  Always shown to the user during review.
- **Review** — the act of accepting or correcting proposals on incoming
  transactions. All incoming transactions require review. The dashboard
  presents them grouped by day; the user may accept transactions
  individually or accept a whole day at once. Corrections feed back into the
  classification system (the flywheel). A reviewed transaction returns to
  the inbox if its source data changes materially (e.g. amount), but not
  cosmetically (e.g. description).
- **Correction log** — the append-only record of every review decision:
  what was proposed, with what provenance, and what the user accepted or
  corrected it to. It is simultaneously the flywheel's memory (few-shot
  context, rule-promotion evidence) and the eval dataset for improving the
  categorization prompt on ever-cheaper models.
- **Rule** — a user-defined condition → action pair applied deterministically
  to incoming transactions (e.g. payee contains "COSTCO" → propose category
  Groceries). Rules take precedence over history and AI. Penny may propose
  new rules from repeated corrections (rule promotion), but a rule is only
  ever created with user consent.

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
- **Balance history** — the per-account time series of balances that powers
  net worth over time.
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
  provider sync (e.g. Plaid), a file import, or manual entry.
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
