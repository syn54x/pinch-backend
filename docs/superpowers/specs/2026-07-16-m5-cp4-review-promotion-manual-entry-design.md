# M5 CP4 — Review surface, rule promotion, manual transaction entry

**Issue:** [#22](https://github.com/syn54x/pinch-backend/issues/22) (sub-issue of PRD M5 #18)
**Branch:** `m5-classification` / PR #23 (accumulating all of M5 — CP4 is the last box)
**Status:** design approved 2026-07-16

## Problem

CP3 built the flywheel's engine — proposals, the correction log, the shared
consume-proposal operation — but no human can turn the wheel: there is no
review surface over HTTP, promotion's `RuleStatus.PROPOSED`/`DISMISSED`
states are unused vocabulary, and hand-entered transactions (the carving gap
the PRD claimed) have no endpoint. CP3 also recorded design debts for this
slice: `PATCH reviewed: true` leaves the pending Proposal row attached,
review payloads need casefold-deduped tags, and three coverage gaps.

## Solution

The human half of the flywheel: two review endpoints wrapping CP3's
`consume_proposal`, consented rule promotion reading the correction log,
manual transaction entry with both birth paths, and the un-review round-trip
— closing CP3's recorded warts along the way.

## Decisions carried in from the grill (PRD #18 — locked)

- `POST /transactions/{id}/review` — body carries the FINAL user data; the
  server diffs against the proposal to record accepted-vs-corrected; empty
  body accepts as-is. Wraps CP3's consume.
- `POST /transactions/review` — explicit ids (≤1,000), accepts as-is,
  answers accepted/skipped counts; already-reviewed ids skip idempotently.
  Never accept-by-filter: reviewing data the user never saw is not review.
- Promotion: inline at review time, scoped to the just-reviewed payee —
  ≥3 user-actor, non-voided log decisions filing payee X as category Y,
  all-time consistency (one deviation kills), no rule in ANY state covering
  X → mint a `payee equals` rule (never `contains`) in `status=proposed`.
  Accept = flip to `active`; dismiss = tombstone, never re-proposed.
  Auto-filed decisions are never evidence. "Latest decision per transaction
  wins" is derived at promotion time, never stored. Promotion reads the
  LOG; history reads transactions.
- Manual entry: `POST /api/v1/transactions`, manual accounts only. Without
  category: ordinary incoming transaction (sweep classifies, inbox shows).
  With category/tags: user data applied, reviewed at birth, log entry with
  an empty proposal (`provenance=none`, the pipeline never ran) and
  `actor=user`. Fingerprint via the M4 recipe so later CSV overlaps flag;
  `source_import` stays null.
- Un-review round-trip: `PATCH reviewed: false` → sweep re-proposes →
  re-review appends a new log entry; earlier entries stand.

## Decisions settled in this brainstorm

- **PATCH `reviewed: true` consumes the pending proposal** (closing CP3's
  recorded wart). One invariant everywhere: *setting reviewed always
  consumes and logs*. The decision snapshot is the transaction's post-PATCH
  user data — the final state IS the decision, no murky diff semantics —
  `actor=user`, and the same inline promotion check runs. The log stays a
  complete record of every review decision; the eval dataset captures
  "pipeline proposed X, user kept Y".
- **`PATCH reviewed: false` defers `classify_ledger`** (after-commit defer,
  the import-commit precedent) so the un-review round-trip is prompt and
  self-contained. The defer fires only on an actual non-null → null
  transition; no-op transitions neither defer nor log.
- **Review responses carry the minted rule.** Single review returns
  `{transaction, result, proposed_rule}`; batch returns
  `{accepted, skipped, proposed_rules}`. The consent moment is the whole
  point of promotion — the UI shows "want a rule?" right then, not after a
  poll. No existing consumer breaks (the frontend repo is empty).
- **Batch review validates-all-first**: any unknown/foreign id → 404 naming
  the missing ids in the error envelope, nothing consumed. `skipped` means
  exactly "already reviewed", never "silently didn't exist".
- **Manual entry takes the full user-data set** — optional `category_id`,
  `tags`, `display_name`, `notes` alongside the source fields. Currency is
  always the account's (a manual account is single-currency by
  construction). Reviewed-at-birth triggers when **category or tags** are
  present; `display_name`/`notes` alone are annotations — the sweep still
  classifies and the row still hits the inbox.
- **DELETE on rules stays uniform.** Dismiss = "never ask again" (the
  tombstone); delete = "forget this ever happened", after which re-proposal
  is possible by design — and deleting a dismissed rule is the only undo
  for a fat-fingered dismiss. Documented and pinned with a test; no
  status-conditional error path.
- Rules accept/dismiss needs **no new surface**: `PATCH /rules/{id}` status
  flips already exist (accept = `status: active`, dismiss =
  `status: dismissed`).

## Components

### 1. Review endpoints — `api/reviews.py`

New module; handlers registered on the transactions router (`/review` and
`/{txn_id:uuid}/review` disambiguate — "review" is not a uuid).

**`POST /api/v1/transactions/{txn_id}/review`** — `ReviewIn`: optional
`category_id`, `tags`, `display_name` (PATCH-style bounds: tags ≤50 names of
≤100 chars, display_name 1–100). **Field-present merge against the
proposal** (`model_fields_set`): absent field = the proposal's value (no
proposal → null/empty), present = the user's final word. Empty body =
accept as-is. `notes` is not reviewable — PATCH's job. `display_name` obeys
consume's contract: applied only when not None (clearing an override is
PATCH's job).

Tags are **casefold-deduped before consume** (trim, first-casing-wins —
the `classify_transaction` precedent), so `decision_tags` logs exactly the
applied set (closes CP3's Task-4 note).

Guards: tenancy 404; body `category_id` 404-checked against the ledger
(the PATCH precedent — consume deliberately doesn't validate membership);
**already-reviewed → 409** ("un-review first" — protects
earlier-entries-stand, no accidental double-log).

Response `ReviewOut`: `{transaction: TransactionOut, result:
"accepted" | "corrected", proposed_rule: RuleOut | null}` — *corrected* iff
final differs from proposal: category id inequality, casefolded tag-set
inequality, or display_name inequality with body-absent/None counting as
accepted. Structured events `review.accepted` / `review.corrected`.

**`POST /api/v1/transactions/review`** — `ReviewBatchIn`: `{ids: [uuid]}`,
1–1,000 (400 outside), duplicates deduped preserving order. Validate all
ids against the ledger in one query; any miss → 404 with the missing ids in
`extra`, nothing consumed. Then per id, in list order: `reviewed_at` set →
`skipped++`; else consume accept-as-is (final = proposal values; missing
proposal → empty accept), `actor=user`, each consume its own DB transaction
— a mid-batch crash leaves cleanly resumable state (retry skips the
consumed). Response `ReviewBatchOut`: `{accepted, skipped, proposed_rules:
[RuleOut]}` — promotion checked once per distinct payee among the accepted,
with Y = that payee's last accepted decision's category (the log votes
decide anyway; inconsistent filings never mint).

### 2. Promotion — `classification/promotion.py`

`maybe_propose_rule(ledger, payee, category_id) -> Rule | None`, called
**after** the consume transaction commits, same request. All evidence from
the log:

1. `category_id` (Y) null → return None (never mint an uncategorize rule).
2. Fetch the ledger's `kind=decision` entries with `input_payee == payee`,
   minus voided ones (entries referenced by a `kind=void` entry's `voids`);
   derive **latest entry per `transaction_id`** (uuid7 id order suffices).
   Keep only `actor=user` latest entries — auto entries are never evidence,
   but a later auto entry supersedes an un-reviewed user decision (that
   transaction simply casts no vote).
3. Require **≥3 votes, every vote's `decision_category_id == Y`**. One
   deviation kills — including a latest decision of NULL (uncategorized was
   a decision).
4. **No rule in ANY state covering the payee**: any ledger rule whose
   condition's payee clause matches payee X under evaluator semantics
   (equals or contains, via the evaluator's own normalization). Rules
   without a payee clause never cover.
5. Mint `Rule(status=PROPOSED, condition = payee equals X (verbatim
   normalized payee), action_category=Y)` — never `contains`, category
   action only (tags/rename are never promotion evidence). Event
   `rule.promoted`.

Failure isolation: a promotion error after consume commits 500s the request
but the review persisted (idempotent retry skips) — no swallowing. Two
same-payee reviews racing could double-mint; documented residual, same
class as CP3's TOCTOU notes (single-tenant, microsecond window).

### 3. PATCH integration — `api/transactions.py`

- `reviewed: true` on an **unreviewed** transaction becomes a consume:
  apply the PATCH's other fields to the row in memory, then
  `consume_proposal` with the post-PATCH final state — `category_id` =
  post-patch value, `tags` = body tags if present else the current tag set,
  `display_name` = the post-patch value (so the log's decision snapshot is
  the whole truth; passing the current value is a same-value no-op apply) —
  `actor=user`, then the same promotion check. PATCH's response stays
  `TransactionOut` (the editor doesn't grow an envelope; a PATCH-minted
  rule surfaces via `GET /rules?status=proposed` + the event — an accepted
  asymmetry, noted here deliberately).
- `reviewed: true` on an already-reviewed transaction: no-op (no log, no
  consume — nothing transitions).
- `reviewed: false` on a **reviewed** transaction clears `reviewed_at` and
  defers `classify_ledger` after the commit; on an unreviewed one, no-op.

### 4. Manual entry — `api/transactions.py`

`POST /api/v1/transactions` (201) — `TransactionCreateIn`: `account_id`,
`date`, `amount_minor`, `description` (1–500 chars), optional `category_id`,
`tags`, `display_name`, `notes` (PATCH bounds). Guards: account tenancy
miss → 404; account with a connection → **409** (manual accounts only —
manual = `connection is None`).

Row construction: currency from the account; `description_normalized` via
`normalize_description`; `fingerprint = compute_fingerprint(account_id,
date, amount_minor, description)` (the M4 recipe — later CSV overlaps flag
against the hand-entered row); `source_import` null.

- **Without category/tags**: create inside a transaction; after commit,
  defer `classify_ledger` (the PRD's "manual creation enqueues
  classification"). The sweep classifies; the inbox shows it.
- **With category or tags**: create, then `consume_proposal` (no proposal
  exists → snapshot records `provenance=none`, the pipeline never ran),
  `actor=user`, promotion check included. Born reviewed — no defer needed.
  Response stays `TransactionOut` (creation endpoint; same accepted
  asymmetry as PATCH). Invariant: no observable state where the row exists
  categorized-but-unlogged — whether create+consume nest in one ferro
  transaction is a plan-time check of ferro's nesting semantics.

Event: `transaction.created`.

### 5. Rules — docstring + tests only

`DELETE /rules/{id}` docstring documents delete-vs-dismiss (delete = forget,
re-proposal possible; dismiss = tombstone, never re-proposed); a test pins
delete-proposed-rule → third filing re-mints. No handler changes.

## Testing

Postgres only (331 green is the baseline); Procrastinate via the autouse
in-memory connector + `run_jobs`; job effects asserted at the HTTP seam; no
live network anywhere.

- **The milestone thesis, one end-to-end HTTP test**: import → commit →
  `run_jobs` → proposals → correct via review → re-import same payee →
  `run_jobs` → history proposes the correction → further consistent filings
  → third consistent → `proposed_rule` in the review response → accept via
  rules PATCH → next arrival → `run_jobs` → `provenance=rule` (rule wins
  precedence).
- **Promotion matrix**: 3 consistent user filings mint; one deviation
  kills; a NULL latest decision kills; auto decisions are never evidence
  (the pollution guard, by name); dismissed tombstone blocks; proposed and
  active rules block; a covering `contains` rule blocks; a rule with no
  payee clause does not block; deleted proposed rule → re-mint possible;
  minted rule is `payee equals` + category action + `status=proposed`.
- **Single review**: empty body accepts as-is (result=accepted); corrected
  body diffs (category / tags / display_name each flip the result);
  field-present merge semantics; tags casefold-dedupe before consume with
  `decision_tags` matching the applied set; review before the sweep ran
  (no proposal → `provenance=none` log entry); 404 tenancy; 404 foreign
  category; 409 already-reviewed.
- **Batch review**: honest counts; already-reviewed skip idempotently;
  unknown id → 404 with ids in extra, nothing consumed; duplicate ids
  deduped; >1,000 → 400; promotion once per distinct payee;
  `proposed_rules` in the response.
- **PATCH integration**: `reviewed: true` consumes (proposal gone, log
  entry with post-PATCH decision snapshot, promotion counts it);
  `reviewed: false` defers the sweep (round-trip: review → un-review →
  `run_jobs` re-proposes → re-review appends a second entry, earlier
  entries stand); no-op transitions neither defer nor log.
- **Manual entry**: uncategorized path (sweep proposes, inbox shows);
  categorized path (reviewed at birth, log entry `provenance=none`
  `actor=user`); tags-only triggers review-at-birth; display_name/notes
  alone do not; connected account 409; foreign account 404; currency from
  account; a later CSV import overlapping the hand-entered row flags the
  duplicate; manual filings count as promotion evidence.
- **CP3 coverage debts closed here**: non-vacuous consume-leaves-notes
  test; auto-filed-decisions-feed-history test; correction-log multi-page
  cursor + combined-filter tests.
- **Tenancy & scope**: user B 404s on review/manual-entry; read-scoped PAT
  403s on every write here (review posts, manual entry).

## Out of scope (CP4)

AI classifier behind the seam, auto-file eligibility for `ai` provenance,
eval harness (M9); webhooks on review/promotion events (M10); explicit rule
priority; retroactive rule application UI; free-text search; transfers
(M6); material-change reopening (M7). PATCH/manual-entry responses growing
promotion envelopes (revisit if the frontend wants the prompt on those
paths too).
