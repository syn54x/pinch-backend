# M5 CP2 — Rules: model, condition evaluator, CRUD, preview

**Issue:** [#20](https://github.com/syn54x/pinch-backend/issues/20) (sub-issue of PRD M5 #18)
**Branch:** `m5-classification` / PR #23 (accumulating all of M5)
**Status:** design approved 2026-07-14

## Problem

Classification's deterministic layer has no home: there is no Rule model, no
place condition semantics live, and no way for a user to see what a rule
would match before creating it. CP3's pipeline needs `matches()` to exist;
CP4's promotion needs the `proposed|dismissed` states to exist; D4's category
delete-block has a dangling forward-reference comment waiting for this table.

## Solution

Deterministic user law, standalone: the `Rule` model (versioned-JSONB
condition, typed action columns), one evaluator module that is the sole
source of condition semantics, rules CRUD, and a capped match preview. Plus
the two CP1-review carryovers that touch the same surfaces.

## Decisions carried in from the grill (PRD #18 — locked)

- Condition vocabulary v0 (D14): `payee equals|contains` (on the normalized
  payee), `amount equals|between` (magnitude in minor units + required
  direction out|in|either + currency defaulting to the user's primary),
  `day_of_month equals|between` (literal calendar matching; between answers
  month-end drift). At most one condition per type, AND-composed, ≥1
  required; OR is "make a second rule".
- One evaluator, three consumers (D14): `matches(condition, transaction)` in
  one module; preview (now) and pipeline/retroactive-apply (CP3) call the
  same function. Adding a condition type later touches the spec model and
  the evaluator, nothing else.
- Actions ride the proposal (D13/D14): rules never write user data directly;
  CP3 applies them. Actions v0: propose category, add tags, rename.
- Rule evaluation order is creation order (uuid7), resolved in exactly one
  `order_by` — the explicit-priority door stays open (D13).
- Status enum `proposed | active | disabled | dismissed`; only `active`
  rules are law; `dismissed` is a promotion tombstone (D15 — CP4 consumes).
- Preview: bare condition payload, capped sample ≤ 50 + truncation flag —
  a sample, not a cursor walk (D14).
- Category delete blocks on rules targeting it, 409 naming them (D4).

## Decisions settled in this brainstorm

- **Conditions as versioned JSONB, actions as typed columns.** The rule:
  open, evolving vocabulary → versioned blob (`MappingSpec` precedent);
  references → real columns. `action_category` is a nullable FK because it
  is the one action with referential-integrity stakes — a dangling category
  id becomes impossible by construction, and the delete-block is one indexed
  query. `action_add_tags` stores tag *names* (resolved at apply-time, CP3 —
  tags stay non-load-bearing, may name not-yet-created tags);
  `action_rename_to` is free text. ≥1 action required. (Ferro's missing
  JSONB filters are NOT the reason — conditions are never queried into, and
  the FK argument holds even when ferro grows them.)
- **User-created rules default to `status=active`** — consent by authorship;
  `proposed` is what CP4's promotion mints.
- **LIKE narrowing skips metacharacter values.** ferro's `like()` has no
  ESCAPE support and SQLite has no default escape char, so portable escaping
  is impossible; instead, a payee value containing `%` or `_` simply skips
  SQL narrowing for that clause and the Python evaluator decides. Narrowing
  is an optimization; `matches()` is correctness.
- **Wire-optional currency, stored-explicit.** `amount.currency` may be
  omitted in requests; create/preview fill it from
  `current_user.primary_currency` before validation/storage, so persisted
  conditions are always explicit.
- **Preview response is `{items, truncated}`**, items in `TransactionOut`
  shape via the CP1 hydrator, promoted from `_out_page` to a public name so
  the rules router doesn't import a private helper.

## Components

### 1. Model — `models.py`
- `RuleStatus(StrEnum)`: `PROPOSED|ACTIVE|DISABLED|DISMISSED`.
- `Rule(TimestampMixin, Model)`: uuid7 id; `ledger` FK (+ `rules` BackRef on
  Ledger); `status: RuleStatus = ACTIVE`; `condition: dict` (validated
  `ConditionSpec`, stored JSONB); `action_category:
  Annotated[Optional[Category], ForeignKey(related_name="rules")] = None`
  (+ `rules` BackRef on Category); `action_add_tags: list[str]` (default
  empty); `action_rename_to: str | None`; timestamps.

### 2. Condition spec — `rules/spec.py` (new package, `imports/` precedent)
- `PayeeCondition{op: Literal["equals","contains"], value: str(1..200)}`.
- `AmountCondition{op: Literal["equals","between"], value: int|None,
  lo: int|None, hi: int|None, direction: Literal["out","in","either"],
  currency: str | None (pattern ^[A-Z]{3}$)}` — magnitudes are positive
  minor units; validator: equals⇔value, between⇔lo≤hi.
- `DayOfMonthCondition{op, value|lo/hi ∈ 1..31, lo≤hi}` (same shape).
- `ConditionSpec{version: Literal[1] = 1, payee?, amount?, day_of_month?}` —
  validator: at least one sub-condition. Unknown version fails the Literal,
  loudly.

### 3. Evaluator — `rules/evaluator.py` (sole source of semantics)
- `matches(spec: ConditionSpec, txn: Transaction) -> bool`:
  - payee: normalize the condition value with the same
    `normalize_description` used for the payee column; `equals` compares,
    `contains` substring-tests against `txn.description_normalized`.
  - amount: `txn.currency == spec.amount.currency` first; direction tests
    the sign of `amount_minor` (out < 0, in > 0, either any); magnitude
    (`abs(amount_minor)`) equals/between.
  - day_of_month: `txn.date.day` equals/between. AND across present clauses.
- `narrow(spec, query) -> query`: best-effort SQL pre-filter — payee `like`
  (`%value%` / exact, only when the normalized value contains no `%`/`_`),
  amount sign-aware ranges (OR-composed for `either`), day_of_month no-op.
- Preview scan: narrowed ledger query in id-keyset batches (500), Python
  `matches()` filter, stop at cap+1. Full-ledger worst case documented as a
  CP1-volumes seam (same class as the tag-filter note).

### 4. Rules API — `api/rules.py`
- `RuleOut`: id, status, condition (spec shape), `action_category:
  {id,name}|null`, action_add_tags, action_rename_to, created_at.
- `POST /api/v1/rules` — validates ConditionSpec (filling currency from
  `current_user.primary_currency` when omitted), in-ledger category 404,
  ≥1 action (400 otherwise).
- `GET /` (Page[T], id-keyset, optional `?status=`), `GET /{id}`,
  `PATCH /{id}` (`model_fields_set` partial; condition replaced whole,
  never merged), `DELETE /{id}` (hard delete — rules carry no history).
- `POST /api/v1/rules/preview` — bare condition body → `{items:
  [TransactionOut…] (≤50), truncated: bool}`. Works before any rule exists.
- Conventions throughout: `current_ledger`, allowlists, tenancy 404s, scope
  guard by construction.

### 5. Category delete-block — `api/categories.py`
After the children check: rules targeting the category (indexed
`action_category_id` query) → 409, detail "retarget or delete the rules
targeting this category first", envelope `extra` carrying `[{id, …}]`.
Replaces the CP2 forward-reference comment. Also: one OpenAPI line on the
DELETE handler noting it carries a JSON body (review follow-up).

### 6. CP1 review carryovers — `api/transactions.py`
- `uncategorized=false` → `category_id != None` (categorized-only), symmetric
  with `reviewed`; no more silent no-op.
- `TransactionOut.tags` sorted by name in the hydrator.
- `_out_page` promoted to a public `hydrate_transactions` (rules preview
  imports it).

## Testing

HTTP seam + model-layer evaluator matrix, both backends. New:
`tests/test_rule_spec.py`, `tests/test_rule_evaluator.py`,
`tests/test_rules_api.py`; extended: `test_categories_api.py`,
`test_transactions_api.py`.

- Evaluator matrix: all six forms; `between 28–31` matches Feb 28; amount
  magnitude + direction + currency isolation (same magnitude, wrong currency
  → no match); payee case/whitespace insensitivity; AND composition; empty
  condition rejected; unknown version rejected loudly.
- Narrowing correctness: a literal `%`/`_` in a payee value still matches
  exactly (narrowing skipped, evaluator decides).
- CRUD: create/list/get/patch/delete; `?status=` filter; tenancy 404s;
  read-PAT 403; Page[T]; condition-replaced-whole on PATCH; ≥1 action
  enforced; foreign category action → 404.
- Preview: import-seam data; matches sampled correctly; 51-match case sets
  `truncated: true` with 50 items; currency default applied.
- Delete-block: category targeted by a rule → 409 naming it; retarget/delete
  rule → delete succeeds.
- Carryovers: `uncategorized=false` returns categorized-only; tags sorted.

## Out of scope (CP2)

Pipeline execution of rules, proposals, retroactive application (CP3);
promotion, accept/dismiss flows (CP4 — the status enum ships now, the
transitions later); explicit rule priority (door held open via the single
`order_by`); amount/account/date condition vocabulary beyond v0.
