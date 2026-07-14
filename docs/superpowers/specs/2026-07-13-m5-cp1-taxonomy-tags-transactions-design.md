# M5 CP1 — Taxonomy, tags, and the transaction user-data surface

**Issue:** [#19](https://github.com/syn54x/pinch-backend/issues/19) (sub-issue of PRD M5 #18)
**Branch:** `m5-classification` (single branch/PR for all of M5; one commit per sub-issue)
**Status:** design approved 2026-07-13

## Problem

M4 shipped transactions with only source data and a docstring reserving the
user-data columns for "M5+". Nothing yet lets a user classify money: no
categories, no tags, no review state, and — because the transaction-list
endpoint was blocked on ferro joins in M4 — no way to read transactions back
at all. CP1 is the foundation every later M5 slice consumes: the editable
taxonomy, free-form tags, the transaction's full user-data surface, and the
date-ordered transaction list.

## Solution

A seeded, fully-editable two-level category taxonomy owned by the ledger;
first-class tags created implicitly by use; the transaction's user-data
columns (category, display name, notes, reviewed state) plus the normalized
payee that CP3's history matching will key on; and `GET/PATCH` over
transactions with a filter surface and date-ordered composite-cursor
pagination.

## Decisions carried in from the grill (PRD #18)

These are locked; CP1 implements them, does not revisit them.

- Uncategorized = NULL category FK; no magic node; accepting an uncategorized
  transaction is legal; NULL never breaks a query (D2).
- Two-level hierarchy, depth cap enforced as **API validation only** — the
  schema encodes no depth, all tree logic is depth-agnostic, raising the cap
  is one constant (D3).
- Category delete requires an explicit `reassign_to` disposition; children
  block; rules-block and proposal-re-point arrive in CP2/CP3 (D4/D5).
- Tags are a two-table design, casefolded uniqueness, implicit creation,
  detach-on-delete (D6).
- Transaction user data: `category`, `display_name` (NULL → show
  `description_raw`), `notes`, `reviewed_at` (NULL = in the inbox) (D7/D8).
- Payee = normalized description, stored + indexed for CP3 (D12).
- Transaction list ordered date desc / id desc behind a composite `(date, id)`
  keyset cursor (D19).

## Codebase decisions settled during brainstorming

- **No backfill for `description_normalized`.** Pinch is pre-deployment (no
  persistent data anywhere); there is no migration framework (ferro
  `auto_migrate`, no alembic). The column is non-null, computed at write; the
  first real deploy runs on an empty schema. The #19 AC's "backfilled for
  existing M4 rows" clause is dropped as targeting rows that cannot exist.
  (I-1: the in-scope guarantee — every transaction has a payee — is fully
  delivered; the excluded case has a stated reason.)
- **Enable ferro `migrate_updates` + `migrate_destructive` in development.**
  Wipe-and-reset is free with no users; auto_migrate may freely alter tables
  as M5 adds columns. Config flags per ADR-0002, defaulting on in dev.
  Alembic is deferred until the schema stabilizes.

## Components

### 1. Migration flags — `settings.py`, `db.py`
Add `database_migrate_updates: bool = True` and
`database_migrate_destructive: bool = True`; thread both through
`connect_database()` into `ferro.connect(...)` (flag names: `migrate_updates`,
`migrate_destructive`).

### 2. Models — `models.py`
- **`Category`**: `ledger` FK; `name`; `parent:
  Annotated[Optional["Category"], ForeignKey(related_name="children")] = None`
  (the verified self-FK spelling); `children` BackRef; composite unique
  `(ledger_id, parent_id, name)`; timestamps.
- **`Tag`**: `ledger` FK; `name`; `name_fold` (casefolded, stored); composite
  unique `(ledger_id, name_fold)`; timestamps.
- **`TransactionTag`**: `ledger` FK (tenancy denorm); `transaction` FK; `tag`
  FK; composite unique `(transaction_id, tag_id)`.
- **`Transaction`** additions: user data `category` FK (nullable),
  `display_name: str | None`, `notes: str | None`, `reviewed_at: datetime |
  None`; source data `description_normalized: str` (non-null, computed at
  write via the existing `normalize_description`), indexed
  `(ledger_id, description_normalized)`.

### 3. Seeding & tree helpers — `taxonomy.py` (new module)
- `seed_default_taxonomy(ledger)` — one `bulk_create`, called inside
  `provision_user`'s existing transaction. Two-level starter set (12 top /
  28 children): Income (Paycheck, Interest, Other Income); Housing
  (Rent/Mortgage, Utilities, Home Improvement); Food & Drink (Groceries,
  Restaurants, Coffee); Transportation (Gas, Parking & Tolls, Public Transit,
  Auto & Ride Share); Shopping (Clothing, Electronics, Household); Health
  (Medical, Pharmacy, Fitness); Entertainment (Streaming, Events, Hobbies);
  Travel (Flights, Lodging, Rideshare); Bills & Subscriptions (Phone,
  Internet, Software); Personal Care; Gifts & Donations; Fees & Charges.
- `MAX_DEPTH = 2` — the sole place the number lives.
- `validate_depth(parent)`, `check_no_cycle(category, new_parent)`,
  `collect_descendants(category_id, all_ledger_cats)` — all walk-until-done,
  none hardcode the depth.

### 4. `/api/v1/categories` — `api/categories.py`
create; list (`Page`, id order); get; `PATCH` (rename + re-parent, running
depth + cycle checks); `DELETE` with required `reassign_to: uuid | null`
(children-block 409; reassign or null transactions). Rules-block (CP2) and
proposal re-point (CP3) marked with forward-reference comments.
`CategoryOut = {id, name, parent_id, created_at}`.

### 5. `/api/v1/tags` — `api/tags.py`
create; list; `DELETE` (detach everywhere). `TagOut = {id, name, created_at}`.

### 6. `/api/v1/transactions` — `api/transactions.py`
- **`PATCH /{id}`** allowlist: `category_id` (null clears), `tags` (list of
  names — implicit-create + reconcile join rows), `display_name`, `notes`,
  `reviewed` (bool → sets/clears `reviewed_at`). Source data untouchable.
- **`GET /` list** filters: `account_id` (repeatable), `date_from`/`date_to`,
  `reviewed`, `category_id` (repeatable, subtree-expanded app-side via
  `collect_descendants`), `uncategorized` (bool → `category_id == None`, no
  join), `tag` (repeatable names, AND-composed). Ordered date desc / id desc.
- **`GET /{id}`** — same Out shape.
- **`TransactionOut`**: full source + user data; `category: {id,name}|null`
  and `tags: [{id,name}]` batch-fetched per page (no N+1, no INNER-join null
  trap). **No `proposal` field** — CP3 adds it additively.

### 7. Composite cursor — `pagination.py`
`paginate_by_date(query, *, cursor, limit)` alongside the id-keyset
`paginate`. Orders `(date desc, id desc)`; predicate
`(date < d) | ((date == d) & (id < i))`; cursor is base64url of
`date.isoformat()|id`, documented opaque, 400 on garbage via the envelope.
The module docstring gains a one-line note about the composite variant.

## Testing

HTTP seam, both backends (sqlite + Postgres), following the M4 test-helper
style. New files: `test_categories_api.py`, `test_tags_api.py`,
`test_transactions_api.py`.

- Seed taxonomy present on signup and fully editable (rename / re-parent /
  delete any seed, nothing breaks).
- Depth-3 create & re-parent → 400; cycle re-parent → 400; depth cap is one
  constant.
- Category delete: reassign-to-id, reassign-to-null (→ uncategorized),
  children-block 409, missing disposition rejected.
- Tags: implicit create on transaction PATCH, casefold dedup, detach on
  delete.
- Transaction PATCH: user-data allowlist honored; source-data fields rejected.
- List filter matrix: each filter, composition, uncategorized survives a
  category-joined query, tag AND-composition; composite cursor stable across
  day boundaries and concurrent inserts.
- Tenancy 404s (never confirming 403); read-scoped PAT 403s on every write.
- No live network anywhere.

## Out of scope (CP1)

Proposals, provenance, the classification pipeline, Procrastinate (CP3);
rules and the rules-block on category delete (CP2); review/promotion/manual
entry (CP4); reporting (M8). The `TransactionOut` and category-delete paths
are designed to grow additively in later CPs.
