# M5 CP3 — Pipeline core: Procrastinate, proposals, correction log, auto-file

**Issue:** [#21](https://github.com/syn54x/pinch-backend/issues/21) (sub-issue of PRD M5 #18)
**Branch:** `m5-classification` / PR #23 (accumulating all of M5)
**Status:** design approved 2026-07-15

## Problem

Pinch can describe money but nothing classifies it. CP1 shipped the taxonomy
and the transaction user-data surface; CP2 shipped rule semantics with no
consumer. There is no background execution (M4's commit docstring promises
classification is "the reacting subsystem's background job, never this
request's"), no Proposal table, no correction log, and the M4 undo contract
("subsystems referencing transactions must tolerate retraction") is a
docstring without an implementation.

## Solution

The flywheel's engine: Procrastinate (ADR-0006, pulled forward to M5 by the
approved epic amendment) running an idempotent per-ledger classification
sweep — active rules → payee history → an abstaining `Classifier` seam —
writing exactly one Proposal per transaction (empty on total abstention),
the append-only self-contained CorrectionLogEntry with its read surface, the
shared consume-proposal operation CP4 will expose over HTTP, and auto-file
for backfills. Import undo and category delete are extended to keep their
contracts over the new tables. sqlite is retired: Postgres is the only
backend, in dev, tests, and CI.

## Decisions carried in from the grill (PRD #18 — locked)

- Execution model (D9-ish): the job is an **idempotent sweep** — classify
  every unreviewed, proposal-less transaction in the ledger. Crashed jobs
  retry; missed ones are swept up next run. Nothing classification-shaped
  runs inside the commit request.
- **Exactly one Proposal per transaction** (unique transaction FK — also the
  double-sweep race guard); the pipeline writes an **empty proposal**
  (`category=NULL, provenance=none`) when every stage abstains — the sweep's
  done-marker. Provenance enum `rule | history | ai | none` names the
  **category's** source; `provenance_detail` JSONB snapshots contributing
  rule ids and the matched transaction id — never FKs.
- Precedence per action type: *category* — first matching active rule that
  sets one (creation order, uuid7, one `order_by`), else history, else AI,
  else empty; *tags* — union of matching rules' tag actions; *rename* —
  first matching rule that renames.
- History: the most recent *reviewed, categorized* transaction in the ledger
  with the same payee (`description_normalized`), from live transactions
  (undo-safe for free); reviewed-but-uncategorized is not a signal.
- AI: a `Classifier` protocol (the `MappingInferrer` precedent); v0
  deterministically abstains — no keys, no network, `provenance=ai`
  unreachable until M9.
- Review **consumes** the proposal: log entry → apply user data → set
  `reviewed_at` → delete the Proposal row, one database transaction. CP3
  builds the shared operation; CP4 exposes it over HTTP. Re-classification
  may replace a proposal only while the transaction is unreviewed.
- `CorrectionLogEntry`: append-only, wide, self-contained — bare
  `transaction_id` (transactions are deletable, the log is forever), input
  snapshot, proposal + decision with category name-snapshots,
  `actor: user | auto`, `kind: decision | void` with `voids` + reason.
  Read surface `GET /api/v1/correction-log` (filters: transaction_id,
  actor, kind). Import undo appends void entries in the same atomic
  transaction; changed minds are new entries.
- Auto-file: `auto_file: true` on import commit — the job applies proposals,
  sets `reviewed_at`, logs `actor=auto`. Safe in v0 by construction (all
  reachable provenances are deterministic applications of the user's own
  precedent). Auto-filed decisions are never promotion evidence.
- Category delete re-points pending proposals at the disposition target, or
  empties them to `provenance=none` on a null disposition.

## Decisions settled in this brainstorm

- **sqlite is retired entirely; Postgres is dev, test, and CI.** ADR-0003
  already made Postgres the only production datastore; Procrastinate makes
  it load-bearing for the product's core loop, and a sqlite shim worker
  would maintain forever a code path nothing deploys on. CP3's concurrency
  acceptance criterion is also untestable on a single-writer backend. Dev
  default flips to local-pg's DSN; conftest drops the sqlite branch
  (throwaway-schema-per-test is the only path, `PINCH_TEST_DATABASE_URL`
  defaulting to the same DSN so `uv run pytest` still just works); ci.yml
  gains a Postgres service container; ADR-0003 gets an amendment note.
- **Auto-file is import-scoped.** The sweep proposes ledger-wide as always,
  but auto-file applies/reviews only transactions with
  `source_import_id == the committed import`. The user consented to skipping
  the inbox for *this backfill*, not for whatever else was pending. The job
  carries `auto_file_import_id`.
- **ProposalTag stores tag *names*, not Tag FKs** — symmetric with
  `Rule.action_add_tags` and the log's name-snapshots. Tag rows are created
  only when a proposal is consumed (`resolve_tags` at apply time): "created
  implicitly on first use" means the user's data actually carries it. A
  rejected proposal leaves no tag debris; tags stay non-load-bearing.
- **History orders by `reviewed_at` desc** (decision recency), tie-break
  `id` desc — not transaction date. A fresh correction of an old
  transaction immediately becomes the payee's signal, which is what "the
  first correction teaches the history stage" (PRD) requires. Same indexed
  payee query either way.
- **No queueing lock on the sweep; an execution lock per ledger.** A
  queueing lock would dedupe an auto-file defer into a pending plain sweep
  and silently drop the flag. The execution lock (`ledger:{id}`) serializes
  sweeps of one ledger to cut unique-violation noise; the unique FK remains
  the correctness guard.
- **Defer after the ferro transaction commits.** A phantom job from a
  rollback is harmless to an idempotent sweep; a job racing data that isn't
  visible yet is not.
- **Wide, flat, typed columns on CorrectionLogEntry** (not JSONB blobs),
  nullable by group so void entries carry only `transaction_id`, `voids`,
  and a reason. Append-only is discipline (no update path anywhere), not
  schema.

## Components

### 1. Postgres-only migration

- `settings.database_url` default →
  `postgres://postgres:password@localhost:5432/postgres` (local-pg works
  out of the box).
- `tests/conftest.py`: sqlite branch removed; `PINCH_TEST_DATABASE_URL`
  falls back to the dev DSN; throwaway schema per test stays as-is.
- `.github/workflows/ci.yml`: `postgres:17` service container with a health
  check; test steps get the DSN via env.
- ADR-0003: short amendment — sqlite dev/test support retired at M5 CP3.
- README / docs / AGENTS: dev setup mentions docker local-pg + the worker
  process; grep-and-fix remaining sqlite mentions.

### 2. Procrastinate wiring — `jobs.py`

- `job_app = procrastinate.App(connector=PsycopgConnector(conninfo=...))` —
  scheme translated `postgres://` → `postgresql://` from
  `settings.database_url`. Procrastinate manages its own tables and
  connections: infrastructure, exempt from block-on-ferro (ADR-0003).
- API lifecycle: `on_startup` opens `job_app.open_async()` alongside ferro;
  `on_shutdown` closes. Enqueue = `classify_ledger.defer_async(...)`.
- Task `classify_ledger(ledger_id: str, auto_file_import_id: str | None)`:
  queue `classification`, retry (5 attempts, exponential), execution lock
  `ledger:{ledger_id}`. Body opens its own ferro session (the worker has no
  request middleware) and calls `classification.pipeline.sweep_ledger`.
- Worker entrypoint: `pinch-dev worker` (cyclopts) — ferro.connect →
  apply Procrastinate's schema when its tables are absent (gated by
  `database_auto_migrate`; hosted deploys use `procrastinate schema
  --apply`) → `run_worker_async()`. Deployment shape: API + worker +
  Postgres.

### 3. Models — `models.py`

- `ProposalProvenance(StrEnum)`: `RULE | HISTORY | AI | NONE`.
- `Proposal(TimestampMixin, Model)`: uuid7 id; `ledger` FK; `transaction`
  FK **unique** (+ BackRef); `category` FK nullable (+ BackRef);
  `proposed_display_name: str | None`; `provenance: ProposalProvenance`;
  `provenance_detail: dict | None`; timestamps.
- `ProposalTag(TimestampMixin, Model)`: uuid7 id; `ledger` FK; `proposal`
  FK; `name: str`; composite unique `(proposal_id, name)`.
- `CorrectionActor(StrEnum)`: `USER | AUTO`. `CorrectionKind(StrEnum)`:
  `DECISION | VOID`.
- `CorrectionLogEntry(TimestampMixin, Model)`: uuid7 id; `ledger` FK;
  `transaction_id: uuid` bare + indexed; `kind`; `actor`; input snapshot
  (`input_description_raw`, `input_payee`, `input_amount_minor`,
  `input_currency`, `input_date`, `input_account_id`); proposal snapshot
  (`proposal_category_id`, `proposal_category_name`,
  `proposal_tags: list[str]`, `proposal_display_name`,
  `proposal_provenance`, `proposal_detail`); decision
  (`decision_category_id`, `decision_category_name`, `decision_tags`,
  `decision_display_name`); void (`voids: uuid | None`,
  `void_reason: str | None`). Snapshot groups nullable; write sites
  validate by kind.

### 4. Classification package — `classification/`

- `classifier.py`: `Classifier` protocol + `AbstainingClassifier` v0 +
  module-level `active_classifier` (the `MappingInferrer` precedent).
- `history.py`: `history_match(ledger_id, payee) -> Transaction | None` —
  same payee, `reviewed_at != None`, `category_id != None`, ledger-wide,
  `order_by reviewed_at desc, id desc`, first row.
- `pipeline.py`: `sweep_ledger(ledger_id, auto_file_import_id=None)` —
  load active rules once (`order_by` id asc — the single creation-order
  site), keyset-iterate unreviewed proposal-less transactions, per
  transaction compose the draft via `rules.evaluator.matches()` + history +
  classifier, write Proposal + ProposalTags in one small transaction; a
  unique violation on the transaction FK means a concurrent sweep won —
  skip. Every abstention still writes the empty proposal. Then, when
  `auto_file_import_id` is set: consume each proposal on that import's
  still-unreviewed transactions with `actor=AUTO`. Structured events:
  `proposal.written`, `import.auto_filed`.
- `consume.py`: `consume_proposal(txn, *, category_id, tags, display_name,
  actor)` — one DB transaction: append the `decision` log entry (input +
  proposal snapshots, category names resolved to snapshots), apply user
  data (`resolve_tags` creates tag rows here), set `reviewed_at`, delete
  the Proposal + its ProposalTags. CP4 wraps this for the review endpoints.

### 5. API surfaces

- `api/imports.py`: `CommitIn.auto_file: bool = False`; commit defers
  `classify_ledger` after its transaction commits. `delete_import`, same
  atomic transaction: delete Proposals (+tags) of the import's
  transactions; append `void` entries for every not-yet-voided `decision`
  entry referencing the deleted transaction ids.
- `api/transactions.py`: `TransactionOut.proposal: ProposalOut | None`
  (`{category: {id, name} | null, tags: [str], display_name, provenance}`);
  `hydrate_transactions` batch-fetches proposals + proposal tags (two more
  queries per page, never per-row).
- `api/correction_log.py`: `GET /api/v1/correction-log` — `Page[T]`,
  id-keyset, filters `transaction_id`, `actor`, `kind`; allowlist out
  model; read-only router registered in app.py.
- `api/categories.py` delete: inside the existing transaction, re-point
  pending Proposals (`category_id = target`) or on null disposition empty
  them (`category_id=None, provenance=NONE, provenance_detail=None`; tags
  and rename survive — they were never the category's decision).

### 6. Settings

No new knobs beyond the flipped `database_url` default. Procrastinate
reuses the domain DSN; worker concurrency stays at the library default.

## Testing

Postgres only. Procrastinate via `job_app.replace_connector(
testing.InMemoryConnector())` in a conftest fixture + a `run_jobs` helper
(`run_worker_async(wait=False, listen_notify=False)`); job effects asserted
at the HTTP seam. No live network anywhere (the abstaining classifier
guarantees it).

- Commit → `run_jobs` → proposals appear with correct provenance; **before**
  `run_jobs` no proposals exist (the request never classifies).
- Precedence matrix: rule vs history vs abstain × category/tags/rename;
  a tags-only rule does not swallow the category decision (history/AI may
  still supply the category, provenance names *its* source); first-rule
  creation order; contributing rule ids in `provenance_detail`.
- Keyless ledger, emptied taxonomy: every transaction gets an empty
  proposal; a second sweep is a no-op (done-marker respected).
- History: most-recent-decision-wins on the payee; reviewed-but-
  uncategorized is not a signal; auto-filed categories participate.
- Auto-file: backfill lands reviewed + categorized via the user's own
  rules/history, `actor=auto`, import-scoped (a pending unrelated inbox
  transaction is proposed but NOT reviewed).
- Undo: proposals gone, log entries voided (never deleted; void entries
  reference the originals with a reason), history no longer matches the
  retracted payee.
- Concurrency: two `sweep_ledger` runs via `asyncio.gather` on real
  Postgres — the unique transaction FK holds, exactly one proposal per
  transaction, both sweeps complete.
- Correction log read surface: filters, pagination, tenancy 404s; consume
  writes decision entries whose snapshots match the applied data.
- Category delete re-point: pending proposals re-pointed / emptied to
  `provenance=none`; delete still blocks on children and targeting rules.
- Consume: log → apply (tag rows minted here, not at proposal time) →
  `reviewed_at` set → proposal deleted, atomically.

## Out of scope (CP3)

Review endpoints, promotion, manual entry, un-review round-trip (CP4 — the
consume operation ships now, its HTTP surface later); retroactive rule
application UI (the evaluator seam exists; CP4+); AI classifier behind the
seam, BYOK, eval harness (M9); webhooks on classification events (M10);
periodic/scheduled sweeps (retries + next-trigger suffice; a periodic task
is one decorator later); ImportRow pruning.
