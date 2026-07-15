# M5 CP3 — Pipeline Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The classification flywheel's engine: Procrastinate background execution, the idempotent per-ledger sweep (rules → history → abstaining classifier), Proposal/ProposalTag/CorrectionLogEntry, the shared consume-proposal operation, auto-file, and the undo/category-delete contract extensions — with sqlite retired and Postgres everywhere.

**Architecture:** A new `classification/` package (the `rules/` precedent) holds the classifier seam, history matching, the pipeline, and consume. A new `jobs.py` owns the Procrastinate app and the `classify_ledger` task; the API defers after commit, a `pinch-dev worker` process executes. Models land in `models.py`; the correction-log read surface is a new read-only router.

**Tech Stack:** Python 3.14, Litestar, ferro-orm 0.16.1, Procrastinate ≥3.9 (PsycopgConnector; `testing.InMemoryConnector` in tests), pydantic v2, pytest (Postgres only).

**Spec:** `docs/superpowers/specs/2026-07-15-m5-cp3-pipeline-design.md`

## Global Constraints

- **Postgres only, from Task 1 on.** Tests run against `PINCH_TEST_DATABASE_URL` defaulting to `postgres://postgres:password@localhost:5432/postgres` (the `local-pg` docker container). There is no sqlite anywhere after Task 1.
- **ferro-orm only** for domain data (ADR-0003, block-on-ferro). Procrastinate manages its own tables/connections — infrastructure, exempt.
- **Every handler reaches domain data via `current_ledger`** (AGENTS I-2); lists return `Page[T]`; tenancy misses answer **404, never 403**; responses are allowlists.
- **Nothing classification-shaped runs inside the commit request** — commit defers; the job classifies. Defer happens **after** the ferro transaction commits.
- **Exactly one Proposal per transaction** (unique transaction FK — the race guard); every stage abstaining still writes the **empty proposal** (`category=NULL, provenance=none`, the done-marker).
- **Precedence per action type**: category = first matching active rule that sets one → history → classifier → none; tags = union of matching rules; rename = first matching renamer. Active rules only; creation order = uuid7 `order_by` id — the pipeline is the **one** site that orders rules.
- **Provenance names the category's source**; `provenance_detail` snapshots ids as **strings**, never FKs.
- **ProposalTag stores names**, not Tag FKs; Tag rows are minted at consume time via `resolve_tags`.
- **CorrectionLogEntry is append-only** (no update path anywhere), wide, self-contained; `transaction_id` is a bare indexed uuid, deliberately not a FK.
- **No live network in tests** — the abstaining classifier guarantees it; Procrastinate via `testing.InMemoryConnector` (autouse).
- **ferro instance-attribute gotchas** (recurring CP1/CP2 bugs): relations are ClassVars — assign shadow FKs (`txn.category_id = x`), never the relation attr; scoped `# ty: ignore[unresolved-attribute]` on instance shadow-FK access; ruff B023 — bind loop/local vars as lambda default args in `where()` predicates; relation traversal renders INNER joins — use column filters for nullable FKs.
- **Multi-step domain writes are atomic** (`async with transaction():`).
- **Validation bounds on every string input.**
- **Commit style**: conventional commits referencing `(M5 CP3, #21)`; body ends `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests per task with `uv run pytest <file> -x -q`; the full suite needs the `local-pg` container running (`docker start local-pg` if stopped).

---

## Task 1: Retire sqlite — Postgres is dev, test, and CI

**Files:**
- Modify: `src/pinch_backend/settings.py:15`
- Modify: `tests/conftest.py`
- Modify: `tests/test_app_lifecycle.py`
- Modify: `tests/test_cli_commands.py:38`
- Modify: `tests/test_imports_api.py:505` (comment only)
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/adr/0003-postgres-and-block-on-ferro.md`
- Modify: `README.md`

**Interfaces:**
- Produces: conftest `DEFAULT_TEST_DATABASE_URL` constant; a `standalone_db_url` fixture (Postgres DSN with a throwaway schema) for tests that run the app's own lifecycle; the `db` fixture is Postgres-only.

- [ ] **Step 1: Flip the settings default**

In `src/pinch_backend/settings.py` replace the `database_url` line:

```python
    database_url: str = "postgres://postgres:password@localhost:5432/postgres"
    """The one datastore (ADR-0003); default matches the local-pg dev
    container. sqlite support was retired at M5 CP3: Procrastinate made
    Postgres load-bearing for the product's core loop, and a backend
    nothing deploys on isn't worth a parallel execution story."""
```

- [ ] **Step 2: Rewrite conftest — Postgres-only db fixture + standalone_db_url**

Replace `tests/conftest.py` wholesale:

```python
import os
import uuid

import pytest
from ferro import connect, engines, execute, reset_engine

DEFAULT_TEST_DATABASE_URL = "postgres://postgres:password@localhost:5432/postgres"
"""The local-pg docker container; CI's service container answers the same
DSN. sqlite was retired at M5 CP3 — Postgres is the only backend."""


def pytest_configure() -> None:
    os.environ.setdefault("LOGFIRE_SEND_TO_LOGFIRE", "false")
    os.environ.setdefault("PINCH_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    # No live network calls in CI (PRD M2): breach-check tests opt back in
    # through a stubbed transport.
    os.environ.setdefault("PINCH_BREACH_CHECK_ENABLED", "false")


def _test_database_url() -> str:
    return os.environ.get("PINCH_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture
async def client(db):
    """The public HTTP seam (PRD M2 onward): the app over the per-test
    database. manage_database=False — the db fixture owns the connection.
    https base_url so the Secure session cookie survives the client's jar."""
    from litestar.testing import AsyncTestClient

    from pinch_backend.api.app import create_app

    async with AsyncTestClient(
        create_app(manage_database=False), base_url="https://testserver.local"
    ) as c:
        yield c


@pytest.fixture
async def db():
    """The model-layer seam: a real Postgres database per test, isolated via
    a throwaway schema (ferro_search_path).

    The import below registers every model table (domain + auth) before
    connect's auto-migration runs, so table creation never depends on which
    test module happened to import the app first. Deferred to fixture time
    because settings must load after pytest_configure's env defaults.
    """
    from pinch_backend import db as _db  # noqa: F401

    postgres_url = _test_database_url()
    schema = f"pinch_test_{uuid.uuid4().hex[:8]}"
    await connect(postgres_url)
    async with engines.session():
        await execute(f'CREATE SCHEMA "{schema}"')
    reset_engine()
    separator = "&" if "?" in postgres_url else "?"
    await connect(f"{postgres_url}{separator}ferro_search_path={schema}", auto_migrate=True)
    async with engines.session():
        yield
        await execute(f'DROP SCHEMA "{schema}" CASCADE')
    reset_engine()


@pytest.fixture
async def standalone_db_url():
    """A Postgres DSN carrying its own throwaway schema, for tests that run
    the app's OWN lifecycle (create_app() with manage_database=True) or the
    CLI's per-command lifespans, instead of the db fixture's ambient session."""
    from pinch_backend import db as _db  # noqa: F401

    postgres_url = _test_database_url()
    schema = f"pinch_standalone_{uuid.uuid4().hex[:8]}"
    await connect(postgres_url)
    async with engines.session():
        await execute(f'CREATE SCHEMA "{schema}"')
    reset_engine()
    separator = "&" if "?" in postgres_url else "?"
    yield f"{postgres_url}{separator}ferro_search_path={schema}"
    await connect(postgres_url)
    async with engines.session():
        await execute(f'DROP SCHEMA "{schema}" CASCADE')
    reset_engine()
```

- [ ] **Step 3: Repoint the standalone-lifecycle tests**

`tests/test_app_lifecycle.py` — the test signature and monkeypatch become:

```python
async def test_the_standalone_app_manages_its_own_database_sessions(
    standalone_db_url, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "database_url", standalone_db_url)
```

(`tmp_path` is no longer used; drop it. Body unchanged.)

`tests/test_cli_commands.py` — in the `cli_env` fixture, replace the sqlite monkeypatch line with:

```python
    monkeypatch.setattr(settings, "database_url", standalone_db_url)
```

and add `standalone_db_url` to the `cli_env` fixture parameters (`def cli_env(tmp_path, monkeypatch, standalone_db_url):`).

`tests/test_imports_api.py:505` — reword the comment: replace `over sqlite's` with wording that names the backend generically, e.g. `over a backend's default` → the line should read that ferro chunks bulk inserts under backend bind-parameter limits (keep the ferro-orm#298 reference, drop the word sqlite).

- [ ] **Step 4: Run the suite against local-pg**

Run: `docker start local-pg 2>/dev/null; uv run pytest -x -q`
Expected: full suite PASSES on Postgres (this is the same suite that passed under `PINCH_TEST_DATABASE_URL` before; only the default changed). If `test_settings.py` asserts the old sqlite default, update that assertion to the new DSN.

- [ ] **Step 5: CI — Postgres service container**

In `.github/workflows/ci.yml`, replace the `test` job with (note: **macOS leaves the matrix** — GitHub service containers are Linux-only, and the backend deploys on Linux; macOS dev still runs local-pg via Docker locally):

```yaml
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: password
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v5

      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.14"
          enable-cache: true

      - name: Install dependencies
        run: uv sync --frozen --all-groups

      - name: Run tests
        env:
          PINCH_TEST_DATABASE_URL: postgres://postgres:password@localhost:5432/postgres
        run: uv run pytest
```

- [ ] **Step 6: ADR amendment + README**

Append to `docs/adr/0003-postgres-and-block-on-ferro.md`:

```markdown
## Amendment (M5 CP3, 2026-07-15)

sqlite dev/test support is retired. Procrastinate (ADR-0006, pulled forward
to M5) made Postgres load-bearing for the product's core loop, and CP3's
concurrency guarantees are untestable on a single-writer backend. Postgres
is dev, test, and CI; the dev default DSN matches the local-pg container.
```

In `README.md`, extend the Development block's intro with the container:

```markdown
Development runs against a local Postgres (`docker run -d --name local-pg
-e POSTGRES_PASSWORD=password -p 5432:5432 postgres:17`); tests isolate
themselves in throwaway schemas.
```

- [ ] **Step 7: Verify no sqlite remains, run suite, commit**

Run: `grep -rn sqlite src tests README.md docs/adr .github` — expected: no hits (docs/superpowers specs/plans may mention it historically; that's fine).
Run: `uv run pytest -x -q` — expected: PASS.

```bash
git add -A
git commit -m "feat(infra): retire sqlite — Postgres is dev, test, and CI (M5 CP3, #21)

Procrastinate makes Postgres load-bearing for the core loop; a backend
nothing deploys on isn't worth a parallel execution story. CI gains a
postgres:17 service container; macOS leaves the test matrix (service
containers are Linux-only; the backend deploys on Linux).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Models — Proposal, ProposalTag, CorrectionLogEntry

**Files:**
- Modify: `src/pinch_backend/models.py`
- Test: `tests/test_classification_models.py` (create)

**Interfaces:**
- Produces: `ProposalProvenance(StrEnum)` = `RULE|HISTORY|AI|NONE`; `CorrectionActor(StrEnum)` = `USER|AUTO`; `CorrectionKind(StrEnum)` = `DECISION|VOID`; `Proposal` (unique `transaction` FK, shadow `transaction_id`; nullable `category` FK; `proposed_display_name`; `provenance`; `provenance_detail: dict | None`); `ProposalTag` (`proposal` FK, `name`, composite-unique `(proposal_id, name)`); `CorrectionLogEntry` (all columns below). BackRefs: `Ledger.proposals/proposal_tags/correction_log_entries`, `Transaction.proposals`, `Category.proposals`, `Proposal.tags`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classification_models.py
"""Proposal / correction-log model invariants (M5 CP3, #21)."""

from datetime import date

import pytest
from ferro import UniqueViolationError

from pinch_backend.models import (
    Account,
    AccountKind,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Transaction,
    provision_user,
)


async def _seed(db) -> tuple[Ledger, Transaction]:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    txn = await Transaction.create(
        ledger=ledger,
        account=account,
        date=date(2026, 7, 1),
        amount_minor=-1250,
        currency="USD",
        description_raw="COSTCO #1234",
        description_normalized="costco #1234",
        fingerprint="fp-1",
    )
    return ledger, txn


async def test_one_proposal_per_transaction_is_schema_enforced(db) -> None:
    ledger, txn = await _seed(db)
    await Proposal.create(ledger=ledger, transaction=txn, provenance=ProposalProvenance.NONE)
    with pytest.raises(UniqueViolationError):
        await Proposal.create(ledger=ledger, transaction=txn, provenance=ProposalProvenance.NONE)


async def test_proposal_round_trips_detail_and_tags(db) -> None:
    ledger, txn = await _seed(db)
    proposal = await Proposal.create(
        ledger=ledger,
        transaction=txn,
        provenance=ProposalProvenance.HISTORY,
        provenance_detail={"matched_transaction_id": "abc"},
        proposed_display_name="Costco",
    )
    await ProposalTag.create(ledger=ledger, proposal=proposal, name="bulk")
    got = await Proposal.get(proposal.id)
    assert got.transaction_id == txn.id
    assert got.category_id is None
    assert got.provenance is ProposalProvenance.HISTORY
    assert got.provenance_detail == {"matched_transaction_id": "abc"}
    with pytest.raises(UniqueViolationError):  # (proposal_id, name) is unique
        await ProposalTag.create(ledger=ledger, proposal=proposal, name="bulk")


async def test_correction_log_entry_round_trips_wide_columns(db) -> None:
    ledger, txn = await _seed(db)
    entry = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn.id,
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
        input_description_raw="COSTCO #1234",
        input_payee="costco #1234",
        input_amount_minor=-1250,
        input_currency="USD",
        input_date=date(2026, 7, 1),
        input_account_id=txn.account_id,
        proposal_provenance=ProposalProvenance.NONE,
        proposal_tags=[],
        decision_tags=["bulk"],
        decision_display_name="Costco",
    )
    got = await CorrectionLogEntry.get(entry.id)
    assert got.transaction_id == txn.id
    assert got.kind is CorrectionKind.DECISION
    assert got.actor is CorrectionActor.USER
    assert got.decision_tags == ["bulk"]
    assert got.voids is None


async def test_void_entry_carries_only_reference_and_reason(db) -> None:
    ledger, txn = await _seed(db)
    decision = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn.id,
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
    )
    void = await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn.id,
        kind=CorrectionKind.VOID,
        actor=CorrectionActor.USER,
        voids=decision.id,
        void_reason="import undone",
    )
    got = await CorrectionLogEntry.get(void.id)
    assert got.voids == decision.id
    assert got.input_description_raw is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classification_models.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'Proposal'`.

- [ ] **Step 3: Add enums and models**

In `src/pinch_backend/models.py`, add the enums near `RuleStatus`:

```python
class ProposalProvenance(StrEnum):
    """Who decided the proposal's CATEGORY (PRD M5 D11/D13): a rule, exact
    payee history, the AI classifier (unreachable until M9's Penny — v0
    deterministically abstains), or nobody (the empty proposal). Contributing
    rules for tags/rename ride in provenance_detail regardless."""

    RULE = "rule"
    HISTORY = "history"
    AI = "ai"
    NONE = "none"


class CorrectionActor(StrEnum):
    """Whose judgment a correction-log decision records: the user's, or the
    system's (auto-file). Auto decisions are never promotion evidence and
    never eval data (PRD M5)."""

    USER = "user"
    AUTO = "auto"


class CorrectionKind(StrEnum):
    """decision = a review consumed a proposal; void = a later entry
    retracting an earlier one (import undo). Voided, never deleted."""

    DECISION = "decision"
    VOID = "void"
```

Add the models after `Rule` (before `provision_user`):

```python
class Proposal(TimestampMixin, Model):
    """The pipeline's suggestion for one transaction (PRD M5 #21): exactly
    one row per transaction — the unique FK is also the double-sweep race
    guard. An empty proposal (category NULL, provenance=none) is the sweep's
    done-marker: every stage abstained, and the abstention is data.

    Review consumes this row (classification.consume): log entry → apply →
    delete, one transaction. The pipeline never proposes over a human
    decision — replacement only while the transaction is unreviewed.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="proposals", index=True)]
    transaction: Annotated[
        "Transaction", ForeignKey(related_name="proposals", unique=True)
    ]
    category: Annotated[Optional["Category"], ForeignKey(related_name="proposals")] = None
    proposed_display_name: str | None = None
    provenance: ProposalProvenance = ProposalProvenance.NONE
    provenance_detail: dict | None = None
    """Snapshots, never FKs (PRD M5 D11): contributing rule ids as strings,
    the matched history transaction id. Survives rule/transaction deletion."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    tags: Relation[list["ProposalTag"]] = BackRef()


class ProposalTag(TimestampMixin, Model):
    """A proposed tag by NAME, not FK (M5 CP3 brainstorm): Tag rows are
    minted only when a proposal is consumed — a rejected proposal leaves no
    tag debris, and tags stay non-load-bearing (CONTEXT.md)."""

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("proposal_id", "name"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="proposal_tags", index=True)]
    proposal: Annotated[Proposal, ForeignKey(related_name="tags", index=True)]
    name: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CorrectionLogEntry(TimestampMixin, Model):
    """One review decision (or its later retraction), append-only and
    self-contained (PRD M5 #21): readable, evaluable, and promotable without
    joining anything deletable. ``transaction_id`` is a bare uuid on purpose
    — transactions are deletable, the log is forever. Snapshot groups are
    nullable: void entries carry only the reference and a reason. Append-only
    is discipline (no code path updates an entry), not schema.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="correction_log_entries", index=True)]
    transaction_id: uuid.UUID = Field(index=True)
    kind: CorrectionKind = CorrectionKind.DECISION
    actor: CorrectionActor = CorrectionActor.USER
    # Input snapshot — what the transaction looked like when decided.
    input_description_raw: str | None = None
    input_payee: str | None = None
    input_amount_minor: int | None = None
    input_currency: str | None = None
    input_date: CalendarDate | None = None
    input_account_id: uuid.UUID | None = None
    # Proposal snapshot — what the pipeline suggested (names, not FKs).
    proposal_category_id: uuid.UUID | None = None
    proposal_category_name: str | None = None
    proposal_tags: list[str] = Field(default_factory=list)
    proposal_display_name: str | None = None
    proposal_provenance: ProposalProvenance | None = None
    proposal_detail: dict | None = None
    # Decision — what the user (or auto-file) actually applied.
    decision_category_id: uuid.UUID | None = None
    decision_category_name: str | None = None
    decision_tags: list[str] = Field(default_factory=list)
    decision_display_name: str | None = None
    # Void bookkeeping (kind=void only).
    voids: uuid.UUID | None = None
    """The entry this one retracts — a bare id, same reasoning as
    transaction_id."""
    void_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
```

Add the BackRefs: on `Ledger` — `proposals: Relation[list["Proposal"]] = BackRef()`, `proposal_tags: Relation[list["ProposalTag"]] = BackRef()`, `correction_log_entries: Relation[list["CorrectionLogEntry"]] = BackRef()`; on `Transaction` — `proposals: Relation[list["Proposal"]] = BackRef()`; on `Category` — `proposals: Relation[list["Proposal"]] = BackRef()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classification_models.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/models.py tests/test_classification_models.py
git commit -m "feat(models): Proposal, ProposalTag, CorrectionLogEntry (M5 CP3, #21)

Unique transaction FK is the double-sweep race guard; proposal tags are
names (minted at consume); the log is wide, self-contained, append-only
with bare uuids where the referent is deletable.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Classifier seam + history matching

**Files:**
- Create: `src/pinch_backend/classification/__init__.py` (empty)
- Create: `src/pinch_backend/classification/classifier.py`
- Create: `src/pinch_backend/classification/history.py`
- Test: `tests/test_classification_stages.py` (create)

**Interfaces:**
- Produces: `Classifier` protocol with `async def classify(self, txn: Transaction) -> uuid.UUID | None`; `AbstainingClassifier`; module global `active_classifier`; `async def history_match(ledger_id: uuid.UUID, payee: str) -> Transaction | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classification_stages.py
"""History matching + the classifier seam (M5 CP3, #21)."""

import uuid
from datetime import UTC, date, datetime

from pinch_backend.classification.classifier import AbstainingClassifier, active_classifier
from pinch_backend.classification.history import history_match
from pinch_backend.models import (
    Account,
    AccountKind,
    Category,
    Ledger,
    Transaction,
    provision_user,
)


async def _seed(db) -> tuple[Ledger, Account, Category]:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    category = await Category.create(ledger=ledger, name="Coffee Shops X")
    return ledger, account, category


async def _txn(ledger, account, payee, *, reviewed_at=None, category=None, day=1):
    return await Transaction.create(
        ledger=ledger,
        account=account,
        date=date(2026, 6, day),
        amount_minor=-500,
        currency="USD",
        description_raw=payee.upper(),
        description_normalized=payee,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        reviewed_at=reviewed_at,
        category=category,
    )


async def test_most_recent_decision_wins(db) -> None:
    ledger, account, coffee = await _seed(db)
    dining = await Category.create(ledger=ledger, name="Dining X")
    # Older transaction, NEWER decision: its category must win (Q4 — history
    # orders by reviewed_at, decision recency, not transaction date).
    await _txn(ledger, account, "starbucks", day=1,
               reviewed_at=datetime(2026, 7, 2, tzinfo=UTC), category=coffee)
    await _txn(ledger, account, "starbucks", day=30,
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=dining)
    hit = await history_match(ledger.id, "starbucks")
    assert hit is not None
    assert hit.category_id == coffee.id


async def test_reviewed_but_uncategorized_is_not_a_signal(db) -> None:
    ledger, account, _ = await _seed(db)
    await _txn(ledger, account, "starbucks",
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=None)
    assert await history_match(ledger.id, "starbucks") is None


async def test_unreviewed_categorized_is_not_a_signal(db) -> None:
    ledger, account, coffee = await _seed(db)
    await _txn(ledger, account, "starbucks", reviewed_at=None, category=coffee)
    assert await history_match(ledger.id, "starbucks") is None


async def test_history_is_ledger_scoped(db) -> None:
    ledger, account, coffee = await _seed(db)
    await _txn(ledger, account, "starbucks",
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee)
    assert await history_match(uuid.uuid7(), "starbucks") is None


async def test_v0_classifier_deterministically_abstains(db) -> None:
    ledger, account, _ = await _seed(db)
    txn = await _txn(ledger, account, "mystery merchant")
    assert isinstance(active_classifier, AbstainingClassifier)
    assert await active_classifier.classify(txn) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classification_stages.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pinch_backend.classification'`.

- [ ] **Step 3: Implement**

`src/pinch_backend/classification/__init__.py` — empty file.

`src/pinch_backend/classification/classifier.py`:

```python
"""The AI-classification seam (PRD M5 D12): a protocol the pipeline depends
on and a deterministic abstainer behind it — the MappingInferrer precedent
exactly. No LLM, no keys, no network: the abstainer is what guarantees CI
never talks to the outside world, and provenance=ai stays unreachable until
M9 swaps Penny in behind the same protocol.
"""

import uuid
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pinch_backend.models import Transaction


class Classifier(Protocol):
    async def classify(self, txn: "Transaction") -> uuid.UUID | None:
        """A category id for ``txn``, or None to abstain; the pipeline never
        asks how."""
        ...


class AbstainingClassifier:
    """v0: always abstains, deterministically."""

    async def classify(self, txn: "Transaction") -> uuid.UUID | None:
        return None


active_classifier: Classifier = AbstainingClassifier()
```

`src/pinch_backend/classification/history.py`:

```python
"""Exact payee history (PRD M5 D12): the most recently DECIDED reviewed,
categorized transaction with the same payee, ledger-wide. Decision recency
(reviewed_at), not transaction date — a fresh correction of an old
transaction immediately becomes the payee's signal (M5 CP3 brainstorm).
Source of truth is live transactions, never the log: undo-safe for free.
Reviewed-but-uncategorized is not a signal (the user shrugged, they didn't
decide).
"""

import uuid

from pinch_backend.models import Transaction


async def history_match(ledger_id: uuid.UUID, payee: str) -> Transaction | None:
    return await (
        Transaction.where(
            lambda t, lid=ledger_id, p=payee: (t.ledger_id == lid)
            & (t.description_normalized == p)
            & (t.reviewed_at != None)  # noqa: E711
            & (t.category_id != None)  # noqa: E711
        )
        .order_by(lambda t: t.reviewed_at, "desc")
        .order_by(lambda t: t.id, "desc")
        .first()
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classification_stages.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/classification tests/test_classification_stages.py
git commit -m "feat(classification): Classifier seam (abstaining v0) + payee history matching (M5 CP3, #21)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: apply_tag_set extraction + consume_proposal

**Files:**
- Modify: `src/pinch_backend/tags.py`
- Modify: `src/pinch_backend/api/transactions.py:250-261` (PATCH tag block)
- Create: `src/pinch_backend/classification/consume.py`
- Test: `tests/test_consume_proposal.py` (create)

**Interfaces:**
- Consumes: models from Task 2; `resolve_tags` (existing).
- Produces: `async def apply_tag_set(ledger: Ledger, txn: Transaction, names: list[str]) -> None` in `tags.py`; `async def consume_proposal(ledger: Ledger, txn: Transaction, *, category_id: uuid.UUID | None, tags: list[str], display_name: str | None, actor: CorrectionActor) -> CorrectionLogEntry` in `classification/consume.py`. Consume is one DB transaction: append decision entry → apply user data → set `reviewed_at` → delete Proposal + ProposalTags. Tolerates a missing proposal (snapshots `provenance=none` — manual entry / pre-sweep review, CP4). Applies `display_name` only when not None (clearing is PATCH's job). Does not touch `notes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consume_proposal.py
"""The shared consume-proposal operation (M5 CP3, #21): log -> apply ->
reviewed_at -> proposal deleted, one transaction. CP4 exposes it over HTTP."""

import uuid
from datetime import date

from pinch_backend.classification.consume import consume_proposal
from pinch_backend.models import (
    Account,
    AccountKind,
    Category,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Tag,
    Transaction,
    TransactionTag,
    provision_user,
)


async def _seed(db):
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    txn = await Transaction.create(
        ledger=ledger,
        account=account,
        date=date(2026, 7, 1),
        amount_minor=-500,
        currency="USD",
        description_raw="STARBUCKS 123",
        description_normalized="starbucks 123",
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )
    return ledger, txn


async def test_consume_applies_logs_and_deletes_atomically(db) -> None:
    ledger, txn = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee Y")
    proposal = await Proposal.create(
        ledger=ledger, transaction=txn, category=coffee,
        provenance=ProposalProvenance.HISTORY,
        provenance_detail={"matched_transaction_id": "m-1"},
        proposed_display_name="Starbucks",
    )
    await ProposalTag.create(ledger=ledger, proposal=proposal, name="treat")

    entry = await consume_proposal(
        ledger, txn,
        category_id=coffee.id, tags=["treat"], display_name="Starbucks",
        actor=CorrectionActor.AUTO,
    )

    got = await Transaction.get(txn.id)
    assert got.category_id == coffee.id
    assert got.display_name == "Starbucks"
    assert got.reviewed_at is not None
    tag = await Tag.where(lambda t: t.name_fold == "treat").first()
    assert tag is not None  # minted at consume, not at proposal time
    assert await TransactionTag.where(lambda tt: tt.transaction_id == txn.id).count() == 1
    assert await Proposal.where(lambda p: p.transaction_id == txn.id).count() == 0
    assert await ProposalTag.where(lambda pt: pt.proposal_id == proposal.id).count() == 0

    assert entry.kind is CorrectionKind.DECISION
    assert entry.actor is CorrectionActor.AUTO
    assert entry.input_payee == "starbucks 123"
    assert entry.proposal_category_id == coffee.id
    assert entry.proposal_category_name == "Coffee Y"
    assert entry.proposal_tags == ["treat"]
    assert entry.proposal_provenance is ProposalProvenance.HISTORY
    assert entry.decision_category_id == coffee.id
    assert entry.decision_category_name == "Coffee Y"
    assert entry.decision_tags == ["treat"]


async def test_consume_corrected_decision_differs_from_proposal(db) -> None:
    ledger, txn = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee Y")
    dining = await Category.create(ledger=ledger, name="Dining Y")
    await Proposal.create(
        ledger=ledger, transaction=txn, category=coffee,
        provenance=ProposalProvenance.RULE, provenance_detail={"rule_ids": ["r-1"]},
    )
    entry = await consume_proposal(
        ledger, txn, category_id=dining.id, tags=[], display_name=None,
        actor=CorrectionActor.USER,
    )
    assert entry.proposal_category_id == coffee.id
    assert entry.decision_category_id == dining.id
    assert (await Transaction.get(txn.id)).category_id == dining.id
    assert (await Transaction.get(txn.id)).display_name is None  # None = leave alone


async def test_consume_without_proposal_snapshots_none(db) -> None:
    ledger, txn = await _seed(db)
    entry = await consume_proposal(
        ledger, txn, category_id=None, tags=[], display_name=None,
        actor=CorrectionActor.USER,
    )
    assert entry.proposal_provenance is ProposalProvenance.NONE
    assert entry.proposal_category_id is None
    assert entry.decision_category_id is None  # accept-as-uncategorized is legal
    assert (await Transaction.get(txn.id)).reviewed_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consume_proposal.py -x -q`
Expected: FAIL — `ModuleNotFoundError` for `pinch_backend.classification.consume`.

- [ ] **Step 3: Extract apply_tag_set and implement consume**

Append to `src/pinch_backend/tags.py` (add `Transaction`, `TransactionTag` to its models import):

```python
async def apply_tag_set(ledger: Ledger, txn: "Transaction", names: list[str]) -> None:
    """Reconcile ``txn``'s tags to exactly ``names``: implicit-create new
    ones, detach removed ones. Shared by the transaction PATCH and the
    consume-proposal operation (M5 CP3) — one reconciliation semantics."""
    wanted = await resolve_tags(ledger, names)
    wanted_ids = {t.id for t in wanted}
    txn_id = txn.id
    existing = await TransactionTag.where(lambda tt, tid=txn_id: tt.transaction_id == tid).all()
    existing_ids = {tt.tag_id for tt in existing}  # ty: ignore[unresolved-attribute]
    for tt in existing:
        if tt.tag_id not in wanted_ids:  # ty: ignore[unresolved-attribute]
            await tt.delete()
    for tg in wanted:
        if tg.id not in existing_ids:
            await TransactionTag.create(ledger=ledger, transaction=txn, tag=tg)
```

In `src/pinch_backend/api/transactions.py`, replace the PATCH handler's `if "tags" in fields:` block body with:

```python
        if "tags" in fields:
            await apply_tag_set(current_ledger, txn, data.tags or [])
```

(import `apply_tag_set` from `pinch_backend.tags`; drop the now-unused `resolve_tags` import and `TransactionTag` import if unreferenced elsewhere in the module.)

Create `src/pinch_backend/classification/consume.py`:

```python
"""Consuming a proposal into user data + the correction log (PRD M5 D13):
log entry -> apply -> reviewed_at -> delete the Proposal, one database
transaction. CP3's auto-file calls this; CP4's review endpoints wrap it.

The caller supplies the FINAL user data (auto-file passes the proposal's own
values; review passes the user's, possibly corrected). Tag rows are minted
here — "created implicitly on first use" means the user's data actually
carries the tag. display_name is applied only when not None: clearing an
override is PATCH's job, not review's. A missing proposal is legal (manual
entry, review before the sweep ran — CP4): the snapshot records
provenance=none, the pipeline never ran.
"""

import uuid

from ferro import transaction

from pinch_backend.models import (
    Category,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger
from pinch_backend.tags import apply_tag_set

log = get_logger(__name__)


async def consume_proposal(
    ledger: Ledger,
    txn: Transaction,
    *,
    category_id: uuid.UUID | None,
    tags: list[str],
    display_name: str | None,
    actor: CorrectionActor,
) -> CorrectionLogEntry:
    txn_id = txn.id
    proposal = await Proposal.where(lambda p, tid=txn_id: p.transaction_id == tid).first()
    proposal_tags: list[str] = []
    if proposal is not None:
        proposal_id = proposal.id
        proposal_tags = [
            pt.name
            for pt in await ProposalTag.where(
                lambda pt, pid=proposal_id: pt.proposal_id == pid
            )
            .order_by(lambda pt: pt.id)
            .all()
        ]

    proposal_category_id = proposal.category_id if proposal else None  # ty: ignore[unresolved-attribute]
    name_ids = sorted({cid for cid in (category_id, proposal_category_id) if cid is not None})
    names = (
        {c.id: c.name for c in await Category.where(lambda c, ids=name_ids: c.id.in_(ids)).all()}
        if name_ids
        else {}
    )

    async with transaction():
        entry = await CorrectionLogEntry.create(
            ledger=ledger,
            transaction_id=txn.id,
            kind=CorrectionKind.DECISION,
            actor=actor,
            input_description_raw=txn.description_raw,
            input_payee=txn.description_normalized,
            input_amount_minor=txn.amount_minor,
            input_currency=txn.currency,
            input_date=txn.date,
            input_account_id=txn.account_id,  # ty: ignore[unresolved-attribute]
            proposal_category_id=proposal_category_id,
            proposal_category_name=names.get(proposal_category_id),
            proposal_tags=proposal_tags,
            proposal_display_name=proposal.proposed_display_name if proposal else None,
            proposal_provenance=proposal.provenance if proposal else ProposalProvenance.NONE,
            proposal_detail=proposal.provenance_detail if proposal else None,
            decision_category_id=category_id,
            decision_category_name=names.get(category_id),
            decision_tags=list(tags),
            decision_display_name=display_name,
        )
        txn.category_id = category_id  # ty: ignore[unresolved-attribute]
        if display_name is not None:
            txn.display_name = display_name
        txn.reviewed_at = utcnow()
        await txn.save()
        await apply_tag_set(ledger, txn, tags)
        if proposal is not None:
            proposal_id = proposal.id
            await ProposalTag.where(lambda pt, pid=proposal_id: pt.proposal_id == pid).delete()
            await proposal.delete()
    log.info(
        "proposal.consumed",
        transaction_id=str(txn.id),
        ledger_id=str(ledger.id),
        actor=actor.value,
        entry_id=str(entry.id),
    )
    return entry
```

- [ ] **Step 4: Run tests (new + PATCH regression)**

Run: `uv run pytest tests/test_consume_proposal.py tests/test_transactions_api.py -x -q`
Expected: PASS (the PATCH tag block now routes through `apply_tag_set`; its existing tests prove the extraction preserved behavior).

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/tags.py src/pinch_backend/api/transactions.py \
        src/pinch_backend/classification/consume.py tests/test_consume_proposal.py
git commit -m "feat(classification): consume-proposal operation + shared tag reconciliation (M5 CP3, #21)

Log -> apply -> reviewed_at -> delete proposal, one transaction. Tag rows
mint at consume, never at proposal time. CP4's review endpoints wrap this.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: The pipeline — classify_transaction + sweep_ledger

**Files:**
- Create: `src/pinch_backend/classification/pipeline.py`
- Test: `tests/test_classification_pipeline.py` (create)

**Interfaces:**
- Consumes: `matches` + `ConditionSpec` (CP2), `history_match`, `active_classifier`, `consume_proposal`, models.
- Produces: `ProposalDraft` dataclass (`category_id`, `provenance`, `detail: dict | None`, `tag_names: list[str]`, `display_name: str | None`); `async def classify_transaction(txn: Transaction, active_rules: list[tuple[Rule, ConditionSpec]]) -> ProposalDraft`; `async def sweep_ledger(ledger_id: uuid.UUID, *, auto_file_import_id: uuid.UUID | None = None) -> None`. Task 6's job calls `sweep_ledger`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classification_pipeline.py
"""Sweep semantics + the precedence matrix at the model seam (M5 CP3, #21).
The HTTP-seam flywheel tests live in test_classification_api.py (Task 7)."""

import asyncio
import uuid
from datetime import UTC, date, datetime

from ferro import engines

from pinch_backend.classification.pipeline import sweep_ledger
from pinch_backend.models import (
    Account,
    AccountKind,
    Category,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Import,
    ImportStatus,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Rule,
    RuleStatus,
    Transaction,
    provision_user,
)


async def _seed(db):
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    return ledger, account


async def _txn(ledger, account, payee, **kwargs):
    defaults = dict(
        date=date(2026, 7, 1),
        amount_minor=-500,
        currency="USD",
        description_raw=payee.upper(),
        description_normalized=payee,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )
    defaults.update(kwargs)
    return await Transaction.create(ledger=ledger, account=account, **defaults)


async def _proposal_for(txn) -> Proposal | None:
    return await Proposal.where(lambda p, tid=txn.id: p.transaction_id == tid).first()


async def test_rule_beats_history_beats_abstention(db) -> None:
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    dining = await Category.create(ledger=ledger, name="Dining P")
    # History says dining...
    await _txn(ledger, account, "starbucks",
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=dining)
    # ...but an active rule says coffee, and rules are law.
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "equals", "value": "starbucks"}},
        action_category=coffee,
    )
    ruled = await _txn(ledger, account, "starbucks")
    history_only = await _txn(ledger, account, "blue bottle")
    await _txn(ledger, account, "blue bottle",
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee)
    nothing = await _txn(ledger, account, "mystery co")

    await sweep_ledger(ledger.id)

    p_rule = await _proposal_for(ruled)
    assert p_rule.provenance is ProposalProvenance.RULE
    assert p_rule.category_id == coffee.id
    assert p_rule.provenance_detail["rule_ids"]  # contributing rules, as strings

    p_hist = await _proposal_for(history_only)
    assert p_hist.provenance is ProposalProvenance.HISTORY
    assert p_hist.category_id == coffee.id
    assert "matched_transaction_id" in p_hist.provenance_detail

    p_none = await _proposal_for(nothing)
    assert p_none.provenance is ProposalProvenance.NONE
    assert p_none.category_id is None


async def test_tags_only_rule_does_not_swallow_the_category(db) -> None:
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    await _txn(ledger, account, "starbucks",
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee)
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "starbucks"}},
        action_add_tags=["treat"],
    )
    txn = await _txn(ledger, account, "starbucks")
    await sweep_ledger(ledger.id)
    p = await _proposal_for(txn)
    # Category came from history; provenance names the CATEGORY's source.
    assert p.provenance is ProposalProvenance.HISTORY
    assert p.category_id == coffee.id
    tags = await ProposalTag.where(lambda pt, pid=p.id: pt.proposal_id == pid).all()
    assert [t.name for t in tags] == ["treat"]
    assert p.provenance_detail["rule_ids"]  # the tags rule still contributed


async def test_first_rule_wins_tags_union_first_rename(db) -> None:
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    dining = await Category.create(ledger=ledger, name="Dining P")
    await Rule.create(  # created first -> wins the category and the rename
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "star"}},
        action_category=coffee, action_add_tags=["a"], action_rename_to="First",
    )
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "bucks"}},
        action_category=dining, action_add_tags=["b", "A"], action_rename_to="Second",
    )
    txn = await _txn(ledger, account, "starbucks")
    await sweep_ledger(ledger.id)
    p = await _proposal_for(txn)
    assert p.category_id == coffee.id
    assert p.proposed_display_name == "First"
    tags = await ProposalTag.where(lambda pt, pid=p.id: pt.proposal_id == pid).all()
    assert sorted(t.name for t in tags) == ["a", "b"]  # union, casefold-deduped


async def test_inactive_rules_are_not_law(db) -> None:
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    for status in (RuleStatus.PROPOSED, RuleStatus.DISABLED, RuleStatus.DISMISSED):
        await Rule.create(
            ledger=ledger, status=status,
            condition={"version": 1, "payee": {"op": "equals", "value": "starbucks"}},
            action_category=coffee,
        )
    txn = await _txn(ledger, account, "starbucks")
    await sweep_ledger(ledger.id)
    assert (await _proposal_for(txn)).provenance is ProposalProvenance.NONE


async def test_sweep_is_idempotent_and_skips_reviewed(db) -> None:
    ledger, account = await _seed(db)
    plain = await _txn(ledger, account, "mystery co")
    reviewed = await _txn(ledger, account, "decided inc",
                          reviewed_at=datetime(2026, 7, 1, tzinfo=UTC))
    await sweep_ledger(ledger.id)
    first = await _proposal_for(plain)
    await sweep_ledger(ledger.id)  # the empty proposal is the done-marker
    assert (await _proposal_for(plain)).id == first.id
    assert await _proposal_for(reviewed) is None
    assert await Proposal.where(lambda p: p.ledger_id == ledger.id).count() == 1


async def test_concurrent_sweeps_hold_the_unique_guard(db) -> None:
    ledger, account = await _seed(db)
    for i in range(25):
        await _txn(ledger, account, f"merchant {i}")

    async def run() -> None:
        async with engines.session():
            await sweep_ledger(ledger.id)

    await asyncio.gather(run(), run())
    assert await Proposal.where(lambda p: p.ledger_id == ledger.id).count() == 25


async def test_auto_file_is_import_scoped(db) -> None:
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    await _txn(ledger, account, "starbucks",
               reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee)
    batch = await Import.create(
        ledger=ledger, account=account, status=ImportStatus.COMMITTED,
        filename="backfill.csv", file_bytes=b"",
    )
    imported = await _txn(ledger, account, "starbucks", source_import=batch)
    bystander = await _txn(ledger, account, "starbucks")  # pending inbox, not this import

    await sweep_ledger(ledger.id, auto_file_import_id=batch.id)

    filed = await Transaction.get(imported.id)
    assert filed.reviewed_at is not None
    assert filed.category_id == coffee.id
    assert await _proposal_for(imported) is None  # consumed
    entry = await CorrectionLogEntry.where(
        lambda e, tid=imported.id: e.transaction_id == tid
    ).first()
    assert entry.actor is CorrectionActor.AUTO
    assert entry.kind is CorrectionKind.DECISION

    watcher = await Transaction.get(bystander.id)
    assert watcher.reviewed_at is None  # proposed, NOT reviewed (Q2)
    assert await _proposal_for(bystander) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classification_pipeline.py -x -q`
Expected: FAIL — no module `pinch_backend.classification.pipeline`.

- [ ] **Step 3: Implement the pipeline**

Create `src/pinch_backend/classification/pipeline.py`:

```python
"""The classification sweep (PRD M5 D9/D13): idempotent, per-ledger,
background-only. Classify every unreviewed, proposal-less transaction —
active rules (creation order) -> exact payee history -> the classifier seam
-> the empty proposal. Precedence is per action type; provenance names the
CATEGORY's source. The unique transaction FK on Proposal is the concurrency
guard: of two racing sweeps, one insert wins and the loser skips.

This module is the ONE site that orders rules (uuid7 creation order) — the
explicit-priority door stays open (D13).
"""

import uuid
from dataclasses import dataclass

from ferro import UniqueViolationError, transaction

from pinch_backend.classification.classifier import active_classifier
from pinch_backend.classification.consume import consume_proposal
from pinch_backend.classification.history import history_match
from pinch_backend.models import (
    CorrectionActor,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Rule,
    RuleStatus,
    Transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.rules.evaluator import matches
from pinch_backend.rules.spec import ConditionSpec

log = get_logger(__name__)

SWEEP_BATCH = 500
"""Keyset batch size for the sweep's transaction walk."""


@dataclass
class ProposalDraft:
    category_id: uuid.UUID | None
    provenance: ProposalProvenance
    detail: dict | None
    tag_names: list[str]
    display_name: str | None


async def classify_transaction(
    txn: Transaction, active_rules: list[tuple[Rule, ConditionSpec]]
) -> ProposalDraft:
    """Compose one transaction's draft. ``active_rules`` arrive pre-ordered
    (creation order); rules contribute tags/rename even when the category
    comes from a later stage — provenance_detail names every contributor."""
    matching = [(rule, spec) for rule, spec in active_rules if matches(spec, txn)]

    tag_names: list[str] = []
    seen_folds: set[str] = set()
    for rule, _ in matching:
        for name in rule.action_add_tags:
            fold = name.strip().casefold()
            if fold and fold not in seen_folds:
                seen_folds.add(fold)
                tag_names.append(name.strip())
    display_name = next(
        (rule.action_rename_to for rule, _ in matching if rule.action_rename_to), None
    )
    detail: dict = {}
    if matching:
        detail["rule_ids"] = [str(rule.id) for rule, _ in matching]

    category_rule = next(
        (rule for rule, _ in matching if rule.action_category_id is not None),  # ty: ignore[unresolved-attribute]
        None,
    )
    if category_rule is not None:
        return ProposalDraft(
            category_id=category_rule.action_category_id,  # ty: ignore[unresolved-attribute]
            provenance=ProposalProvenance.RULE,
            detail=detail,
            tag_names=tag_names,
            display_name=display_name,
        )

    hit = await history_match(txn.ledger_id, txn.description_normalized)  # ty: ignore[unresolved-attribute]
    if hit is not None:
        detail["matched_transaction_id"] = str(hit.id)
        return ProposalDraft(
            category_id=hit.category_id,  # ty: ignore[unresolved-attribute]
            provenance=ProposalProvenance.HISTORY,
            detail=detail,
            tag_names=tag_names,
            display_name=display_name,
        )

    ai_category = await active_classifier.classify(txn)
    if ai_category is not None:
        return ProposalDraft(
            category_id=ai_category,
            provenance=ProposalProvenance.AI,
            detail=detail or None,
            tag_names=tag_names,
            display_name=display_name,
        )

    return ProposalDraft(
        category_id=None,
        provenance=ProposalProvenance.NONE,
        detail=detail or None,
        tag_names=tag_names,
        display_name=display_name,
    )


async def sweep_ledger(
    ledger_id: uuid.UUID, *, auto_file_import_id: uuid.UUID | None = None
) -> None:
    """The idempotent sweep. Safe to run twice, safe to run concurrently,
    safe to crash and re-run: progress is the proposals themselves."""
    ledger = await Ledger.get(ledger_id)
    rules = (
        await Rule.where(
            lambda r, lid=ledger_id: (r.ledger_id == lid) & (r.status == RuleStatus.ACTIVE)
        )
        .order_by(lambda r: r.id)
        .all()
    )
    active_rules = [(rule, ConditionSpec(**rule.condition)) for rule in rules]

    written = 0
    last_id: uuid.UUID | None = None
    while True:
        query = Transaction.where(
            lambda t, lid=ledger_id: (t.ledger_id == lid) & (t.reviewed_at == None)  # noqa: E711
        )
        if last_id is not None:
            query = query.where(lambda t, after=last_id: t.id > after)
        batch = await query.order_by(lambda t: t.id).limit(SWEEP_BATCH).all()
        if not batch:
            break
        last_id = batch[-1].id
        batch_ids = [t.id for t in batch]
        proposed = {
            p.transaction_id  # ty: ignore[unresolved-attribute]
            for p in await Proposal.where(
                lambda p, ids=batch_ids: p.transaction_id.in_(ids)
            ).all()
        }
        for txn in batch:
            if txn.id in proposed:
                continue
            draft = await classify_transaction(txn, active_rules)
            try:
                async with transaction():
                    # Shadow-FK kwarg (category_id): runtime-synthesized and
                    # invisible to ty (ferro PRD 0004 / ferro-orm#290).
                    proposal = await Proposal.create(
                        ledger=ledger,
                        transaction=txn,
                        category_id=draft.category_id,  # ty: ignore[unknown-argument]
                        proposed_display_name=draft.display_name,
                        provenance=draft.provenance,
                        provenance_detail=draft.detail,
                    )
                    for name in draft.tag_names:
                        await ProposalTag.create(ledger=ledger, proposal=proposal, name=name)
            except UniqueViolationError:
                continue  # a concurrent sweep won this transaction
            written += 1
            log.info(
                "proposal.written",
                transaction_id=str(txn.id),
                ledger_id=str(ledger_id),
                provenance=draft.provenance.value,
            )

    auto_filed = 0
    if auto_file_import_id is not None:
        import_id = auto_file_import_id
        last_id = None
        while True:
            query = Transaction.where(
                lambda t, iid=import_id: (t.source_import_id == iid)
                & (t.reviewed_at == None)  # noqa: E711
            )
            if last_id is not None:
                query = query.where(lambda t, after=last_id: t.id > after)
            batch = await query.order_by(lambda t: t.id).limit(SWEEP_BATCH).all()
            if not batch:
                break
            last_id = batch[-1].id
            for txn in batch:
                txn_id = txn.id
                proposal = await Proposal.where(
                    lambda p, tid=txn_id: p.transaction_id == tid
                ).first()
                if proposal is None:
                    continue  # another sweep is mid-write; the retry sweeps it
                proposal_id = proposal.id
                tag_names = [
                    pt.name
                    for pt in await ProposalTag.where(
                        lambda pt, pid=proposal_id: pt.proposal_id == pid
                    )
                    .order_by(lambda pt: pt.id)
                    .all()
                ]
                await consume_proposal(
                    ledger,
                    txn,
                    category_id=proposal.category_id,  # ty: ignore[unresolved-attribute]
                    tags=tag_names,
                    display_name=proposal.proposed_display_name,
                    actor=CorrectionActor.AUTO,
                )
                auto_filed += 1
        log.info(
            "import.auto_filed",
            import_id=str(auto_file_import_id),
            ledger_id=str(ledger_id),
            transactions=auto_filed,
        )

    log.info(
        "classification.sweep_completed",
        ledger_id=str(ledger_id),
        proposals_written=written,
        auto_filed=auto_filed,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_classification_pipeline.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/classification/pipeline.py tests/test_classification_pipeline.py
git commit -m "feat(classification): idempotent sweep — rules > history > classifier > empty (M5 CP3, #21)

Per-action-type precedence, provenance names the category's source, the
unique FK absorbs concurrent sweeps, auto-file is import-scoped.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Procrastinate — jobs.py, lifecycle, worker, test fixtures

**Files:**
- Modify: `pyproject.toml` (dependency)
- Create: `src/pinch_backend/jobs.py`
- Modify: `src/pinch_backend/api/app.py`
- Modify: `src/pinch_backend/cli/app.py`
- Modify: `tests/conftest.py` (job fixtures)
- Test: `tests/test_jobs.py` (create)

**Interfaces:**
- Consumes: `sweep_ledger` (Task 5).
- Produces: `job_app` (procrastinate App); task `classify_ledger(ledger_id: str, auto_file_import_id: str | None = None)` (name `classification.classify_ledger`, queue `classification`); `open_job_app()` / `close_job_app()` lifecycle hooks; `run_worker()` coroutine + `pinch-dev worker` command; conftest **autouse** `job_connector` fixture (InMemoryConnector) and `run_jobs` fixture. Task 7 defers via `classify_ledger.configure(lock=...).defer_async(...)`.

- [ ] **Step 1: Add the dependency**

Run: `uv add "procrastinate>=3.9"`
Expected: `procrastinate` and `psycopg[pool]` land in `pyproject.toml` / `uv.lock`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_jobs.py
"""Procrastinate wiring (M5 CP3, #21): the app, the task, the conninfo
translation. Job effects on real data are asserted at the HTTP seam in
test_classification_api.py."""

import uuid
from datetime import date

from pinch_backend.jobs import _psycopg_conninfo, classify_ledger, job_app
from pinch_backend.models import (
    Account,
    AccountKind,
    Ledger,
    Proposal,
    Transaction,
    provision_user,
)


def test_conninfo_translation_strips_ferro_params() -> None:
    assert (
        _psycopg_conninfo("postgres://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    assert (
        _psycopg_conninfo("postgres://u:p@h:5432/db?ferro_search_path=s&sslmode=require")
        == "postgresql://u:p@h:5432/db?sslmode=require"
    )


async def test_deferred_job_sweeps_the_ledger(db, job_connector, run_jobs) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Checking")
    await Transaction.create(
        ledger=ledger, account=account, date=date(2026, 7, 1), amount_minor=-500,
        currency="USD", description_raw="X", description_normalized="x",
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
    )

    await classify_ledger.configure(lock=f"ledger:{ledger.id}").defer_async(
        ledger_id=str(ledger.id)
    )
    assert len(job_connector.jobs) == 1
    assert await Proposal.where(lambda p: p.ledger_id == ledger.id).count() == 0

    await run_jobs()
    assert await Proposal.where(lambda p: p.ledger_id == ledger.id).count() == 1


def test_task_is_registered_under_its_stable_name() -> None:
    assert "classification.classify_ledger" in job_app.tasks
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_jobs.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pinch_backend.jobs'` (and missing fixtures).

- [ ] **Step 4: Implement jobs.py**

Create `src/pinch_backend/jobs.py`:

```python
"""Background jobs on Procrastinate (ADR-0006, pulled forward to M5 by the
approved epic amendment): Postgres-native queue, LISTEN/NOTIFY, retries,
locks. Deployment shape: one API process (defers), one worker process
(executes), one Postgres. Procrastinate manages its own tables and
connections — infrastructure, exempt from block-on-ferro (ADR-0003).

The API defers AFTER its ferro transaction commits: a phantom job from a
rollback is harmless to an idempotent sweep; a job racing data that isn't
visible yet is not. Tests replace the connector with
procrastinate.testing.InMemoryConnector (conftest, autouse).
"""

import uuid

import procrastinate
from ferro import engines

from pinch_backend.settings import settings


def _psycopg_conninfo(url: str) -> str:
    """ferro's DSN uses the postgres:// scheme and may carry ferro-only
    query params (ferro_search_path); psycopg wants postgresql:// and
    server params only."""
    _, _, rest = url.partition("://")
    base, _, query = rest.partition("?")
    params = [p for p in query.split("&") if p and not p.startswith("ferro_")]
    suffix = f"?{'&'.join(params)}" if params else ""
    return f"postgresql://{base}{suffix}"


job_app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=_psycopg_conninfo(settings.database_url))
)


async def open_job_app() -> None:
    await job_app.open_async()


async def close_job_app() -> None:
    await job_app.close_async()


async def ensure_job_schema() -> None:
    """Apply Procrastinate's schema when its tables are absent — the same
    config-not-fork stance as ferro's auto_migrate; hosted deploys disable
    the flag and run `procrastinate schema --apply` themselves."""
    if not settings.database_auto_migrate:
        return
    if await job_app.job_manager.check_connection_async():
        return
    await job_app.schema_manager.apply_schema_async()


@job_app.task(
    name="classification.classify_ledger",
    queue="classification",
    retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=2),
)
async def classify_ledger(ledger_id: str, auto_file_import_id: str | None = None) -> None:
    """The idempotent per-ledger sweep (PRD M5 D9). Args are strings —
    Procrastinate job payloads are JSON. Deferred with lock=ledger:{id} so
    two sweeps of one ledger serialize (the unique Proposal FK stays the
    correctness guard; the lock just cuts violation noise)."""
    from pinch_backend.classification.pipeline import sweep_ledger

    async with engines.session():
        await sweep_ledger(
            uuid.UUID(ledger_id),
            auto_file_import_id=uuid.UUID(auto_file_import_id) if auto_file_import_id else None,
        )


async def run_worker() -> None:
    """The worker process: ferro + the queue, then work until signalled."""
    from pinch_backend.db import connect_database

    await connect_database()
    async with job_app.open_async():
        await ensure_job_schema()
        await job_app.run_worker_async()
```

- [ ] **Step 5: Wire the API lifecycle and the worker command**

In `src/pinch_backend/api/app.py`: import `close_job_app, open_job_app` from `pinch_backend.jobs`, and change the lifecycle lines in `create_app` to:

```python
        on_startup=[connect_database, open_job_app] if manage_database else [],
        on_shutdown=[close_job_app, disconnect_database] if manage_database else [],
```

(The `manage_database=False` test path skips both: tests defer through the replaced in-memory connector, which needs no open.)

In `src/pinch_backend/cli/app.py`, add:

```python
@app.command
def worker() -> None:
    """Run the background-job worker (deployment shape: API + worker +
    Postgres, ADR-0006). Applies Procrastinate's schema on first run when
    PINCH_DATABASE_AUTO_MIGRATE is on."""
    import asyncio

    from pinch_backend.jobs import run_worker

    asyncio.run(run_worker())
```

- [ ] **Step 6: Add conftest job fixtures**

Append to `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def job_connector():
    """Every test runs Procrastinate on the in-memory connector — same
    stance as "no live network": nothing in the suite touches a real queue.
    Yields the connector; inspect queued jobs via `job_connector.jobs`."""
    from procrastinate import testing

    from pinch_backend.jobs import job_app

    in_memory = testing.InMemoryConnector()
    with job_app.replace_connector(in_memory):
        yield in_memory


@pytest.fixture
def run_jobs(job_connector):
    """Execute everything queued, then return (the testing-connector
    pattern): job effects are asserted back at the API seam."""
    from pinch_backend.jobs import job_app

    async def _run() -> None:
        await job_app.run_worker_async(
            wait=False, listen_notify=False, install_signal_handlers=False
        )

    return _run
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_jobs.py -x -q`
Expected: PASS.
Run: `uv run pytest tests/test_app_lifecycle.py tests/test_cli_commands.py -x -q`
Expected: PASS — the standalone-lifecycle tests now also open the job app; the autouse fixture keeps it in-memory. If the `cli reference docs` prek hook regenerates CLI docs for the new `worker` command, `git add` the regenerated files.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/pinch_backend/jobs.py src/pinch_backend/api/app.py \
        src/pinch_backend/cli/app.py tests/conftest.py tests/test_jobs.py
git commit -m "feat(jobs): Procrastinate app, classify_ledger task, worker entrypoint (M5 CP3, #21)

ADR-0006 lands: API defers, pinch-dev worker executes, one Postgres.
Tests run the in-memory connector autouse — no real queue anywhere.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: Commit wiring — auto_file flag + defer, HTTP-seam flywheel tests

**Files:**
- Modify: `src/pinch_backend/api/imports.py` (CommitIn + commit_import)
- Test: `tests/test_classification_api.py` (create)

**Interfaces:**
- Consumes: `classify_ledger` (Task 6).
- Produces: `CommitIn.auto_file: bool = False`; commit defers `classify_ledger` with `lock=ledger:{id}` and `auto_file_import_id` when the flag is set — **after** the transaction block.

- [ ] **Step 1: Write the failing test**

The three flywheel tests depend on `TransactionOut.proposal` (Task 8) and `GET /correction-log` (Task 9); they land here marked `@pytest.mark.xfail(strict=True)` so every commit stays green — Tasks 8 and 9 each remove the xfail they satisfy. The two enqueue tests are live now.

```python
# tests/test_classification_api.py
"""The flywheel at the HTTP seam (M5 CP3, #21): commit -> job -> proposals
with provenance; auto-file; undo retraction; the commit request itself
never classifies. Data flows through the real M4 import seam."""

import pytest

TX = "/api/v1/transactions"
IMPORTS = "/api/v1/imports"
CATEGORIES = "/api/v1/categories"
RULES = "/api/v1/rules"
LOG = "/api/v1/correction-log"
PASSWORD = "correct horse battery staple"
MAPPING = {
    "delimiter": ",",
    "has_header": True,
    "date_column": 0,
    "date_format": "%Y-%m-%d",
    "amount_column": 1,
    "description_columns": [2],
}
CSV_ROWS = [
    ("2026-07-01", "-5.00", "STARBUCKS 123"),
    ("2026-07-02", "-42.00", "MYSTERY CO"),
]


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com") -> None:
    r = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text


async def _account(client) -> str:
    r = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _commit_csv(client, account_id, *, rows=CSV_ROWS, auto_file=False) -> str:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{m}" for d, a, m in rows) + "\n"
    up = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
    import_id = up.json()["id"]
    confirmed = await client.post(
        f"{IMPORTS}/{import_id}/mapping", json=MAPPING, headers=await _csrf(client)
    )
    assert confirmed.status_code == 200, confirmed.text
    committed = await client.post(
        f"{IMPORTS}/{import_id}/commit",
        json={"auto_file": auto_file},
        headers=await _csrf(client),
    )
    assert committed.status_code == 200, committed.text
    return import_id


async def _category(client, name: str) -> str:
    r = await client.post(CATEGORIES, json={"name": name}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _rule(client, *, contains: str, category_id: str) -> None:
    r = await client.post(
        RULES,
        json={
            "condition": {"payee": {"op": "contains", "value": contains}},
            "action_category_id": category_id,
        },
        headers=await _csrf(client),
    )
    assert r.status_code == 201, r.text


async def _transactions(client) -> list[dict]:
    r = await client.get(TX)
    assert r.status_code == 200
    return r.json()["items"]


async def test_commit_enqueues_exactly_one_classification_job(client, job_connector) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    jobs = list(job_connector.jobs.values())
    assert len(jobs) == 1
    assert jobs[0]["task_name"] == "classification.classify_ledger"
    assert jobs[0]["args"]["auto_file_import_id"] is None
    assert jobs[0]["lock"] == f"ledger:{jobs[0]['args']['ledger_id']}"


async def test_auto_file_commit_carries_the_import_id(client, job_connector) -> None:
    await _signup(client)
    account_id = await _account(client)
    import_id = await _commit_csv(client, account_id, auto_file=True)
    jobs = list(job_connector.jobs.values())
    assert len(jobs) == 1
    assert jobs[0]["args"]["auto_file_import_id"] == import_id


@pytest.mark.xfail(reason="TransactionOut.proposal lands in Task 8", strict=True)
async def test_commit_defers_and_the_job_writes_proposals(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee Z")
    await _rule(client, contains="starbucks", category_id=coffee)

    await _commit_csv(client, account_id)
    for txn in await _transactions(client):
        assert txn["proposal"] is None  # the commit request never classifies

    await run_jobs()
    by_payee = {t["description_normalized"]: t for t in await _transactions(client)}
    ruled = by_payee["starbucks 123"]["proposal"]
    assert ruled["provenance"] == "rule"
    assert ruled["category"]["name"] == "Coffee Z"
    unknown = by_payee["mystery co"]["proposal"]
    assert unknown["provenance"] == "none"
    assert unknown["category"] is None


@pytest.mark.xfail(reason="correction-log endpoint lands in Task 9", strict=True)
async def test_auto_file_lands_reviewed_and_logged_auto(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee Z")
    await _rule(client, contains="starbucks", category_id=coffee)
    await _commit_csv(client, account_id, auto_file=True)
    await run_jobs()

    by_payee = {t["description_normalized"]: t for t in await _transactions(client)}
    filed = by_payee["starbucks 123"]
    assert filed["reviewed_at"] is not None
    assert filed["category"]["name"] == "Coffee Z"
    assert filed["proposal"] is None  # consumed
    unknown = by_payee["mystery co"]
    assert unknown["reviewed_at"] is not None  # the empty proposal auto-files too
    assert unknown["category"] is None

    entries = (await client.get(LOG)).json()["items"]
    assert entries and all(e["actor"] == "auto" for e in entries)


@pytest.mark.xfail(reason="TransactionOut.proposal lands in Task 8", strict=True)
async def test_keyless_empty_taxonomy_sweeps_clean(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    # Empty the seeded taxonomy: children first, then roots.
    cats = (await client.get(f"{CATEGORIES}?limit=100")).json()["items"]
    for c in [c for c in cats if c["parent_id"]] + [c for c in cats if not c["parent_id"]]:
        r = await client.request(
            "DELETE",
            f"{CATEGORIES}/{c['id']}",
            json={"reassign_to": None},
            headers=await _csrf(client),
        )
        assert r.status_code == 204, r.text

    await _commit_csv(client, account_id)
    await run_jobs()
    for txn in await _transactions(client):
        assert txn["proposal"]["provenance"] == "none"

    # Re-defer: the sweep does not reprocess (empty proposals are done-markers).
    await _commit_csv(client, account_id, rows=[("2026-07-03", "-1.00", "ANOTHER")])
    await run_jobs()
    payees = {t["description_normalized"] for t in await _transactions(client)}
    assert "another" in payees
```

- [ ] **Step 2: Run test to verify the non-xfail tests fail**

Run: `uv run pytest tests/test_classification_api.py -x -q`
Expected: `test_commit_enqueues_exactly_one_classification_job` FAILS — `CommitIn` rejects `auto_file` / no job queued. xfail tests report xfail.

- [ ] **Step 3: Wire the commit**

In `src/pinch_backend/api/imports.py`:

Add to `CommitIn`:

```python
    auto_file: bool = False
    """Backfill mode (story 12): the classification job applies each of this
    import's proposals immediately — reviewed and categorized by the user's
    own rules/history, logged actor=auto, never promotion evidence. Scoped
    to THIS import; anything already in the inbox stays there."""
```

In `commit_import`, import `classify_ledger` from `pinch_backend.jobs`, and after the `async with transaction():` block (before the `log.info`):

```python
    # Classification is the reacting subsystem's background job, never this
    # request's (M4's bound contract). Deferred AFTER the commit transaction:
    # the job must see the rows; a phantom job from a rollback would have
    # been harmless (idempotent sweep), invisible data would not.
    await classify_ledger.configure(lock=f"ledger:{current_ledger.id}").defer_async(
        ledger_id=str(current_ledger.id),
        auto_file_import_id=str(batch.id) if data.auto_file else None,
    )
```

Add `auto_file=data.auto_file` to the `import.committed` log event.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_classification_api.py tests/test_imports_api.py -x -q`
Expected: PASS (xfails stay xfail; existing import tests still green — the autouse connector absorbs the new defer).

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/imports.py tests/test_classification_api.py
git commit -m "feat(api): import commit enqueues classification; auto_file flag (M5 CP3, #21)

Deferred after the commit transaction with a per-ledger execution lock;
the request itself never classifies (M4's bound contract).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: TransactionOut.proposal — hydrator extension

**Files:**
- Modify: `src/pinch_backend/api/transactions.py`
- Test: `tests/test_classification_api.py` (remove xfail from `test_commit_defers_and_the_job_writes_proposals` and `test_keyless_empty_taxonomy_sweeps_clean`), `tests/test_transactions_api.py` (one addition)

**Interfaces:**
- Produces: `ProposalOut{category: CategoryRef | None, tags: list[str], display_name: str | None, provenance: ProposalProvenance}`; `TransactionOut.proposal: ProposalOut | None` — additive, hydrated batch-wise.

- [ ] **Step 1: Un-xfail + add the hydration shape tests**

Remove the `@pytest.mark.xfail` from `test_commit_defers_and_the_job_writes_proposals` and `test_keyless_empty_taxonomy_sweeps_clean` in `tests/test_classification_api.py`. Append to `tests/test_transactions_api.py` (its `_signup`/`_account`/`_import` helpers already exist at the top of the file):

```python
async def test_transaction_without_proposal_serializes_null(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "PLAIN")])
    txn = (await client.get(TX)).json()["items"][0]
    assert txn["proposal"] is None


async def test_pending_proposal_inlines_sorted_tags(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "TAGGED")])
    # The pending proposal is pipeline-owned state; seed it at the model
    # layer — the surface under test is the list's hydration.
    from pinch_backend.models import (
        Ledger,
        Proposal,
        ProposalProvenance,
        ProposalTag,
        Transaction,
    )

    ledger = (await Ledger.all())[0]
    txn = (await Transaction.all())[0]
    proposal = await Proposal.create(
        ledger=ledger, transaction=txn, provenance=ProposalProvenance.NONE
    )
    for name in ["zeta", "Alpha"]:
        await ProposalTag.create(ledger=ledger, proposal=proposal, name=name)
    out = (await client.get(TX)).json()["items"][0]["proposal"]
    assert out["tags"] == ["Alpha", "zeta"]  # sorted case-insensitively
    assert out["provenance"] == "none"
    assert out["category"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_classification_api.py -q`
Expected: FAIL — `KeyError: 'proposal'` (the field doesn't exist yet).

- [ ] **Step 3: Extend the hydrator**

In `src/pinch_backend/api/transactions.py`: import `Proposal`, `ProposalProvenance`, `ProposalTag` from models. Add after `TagRef`:

```python
class ProposalOut(BaseModel):
    """The pending pipeline suggestion riding the transaction (M5 CP3) —
    enough for the inbox to render from the list alone."""

    category: CategoryRef | None
    tags: list[str]
    display_name: str | None
    provenance: ProposalProvenance
```

Add `proposal: ProposalOut | None` to `TransactionOut` (after `tags`), and update its docstring (the "added additively in CP3" sentence is now history — reword to name the field).

In `hydrate_transactions`, after the `links` fetch add the proposal batch (and fold proposal category ids into the category fetch — move the `cats` query AFTER computing proposal category ids):

```python
    proposals = (
        await Proposal.where(lambda p, ids=txn_ids: p.transaction_id.in_(ids)).all()
        if txn_ids
        else []
    )
    by_txn_proposal = {p.transaction_id: p for p in proposals}  # ty: ignore[unresolved-attribute]
    proposal_ids = [p.id for p in proposals]
    proposal_tag_rows = (
        await ProposalTag.where(lambda pt, ids=proposal_ids: pt.proposal_id.in_(ids)).all()
        if proposal_ids
        else []
    )
    tags_by_proposal: dict[uuid.UUID, list[str]] = {}
    for pt in sorted(proposal_tag_rows, key=lambda pt: pt.name.casefold()):
        tags_by_proposal.setdefault(pt.proposal_id, []).append(pt.name)  # ty: ignore[unresolved-attribute]
```

Extend `cat_ids` to include proposal categories:

```python
    cat_ids = sorted(
        {t.category_id for t in txns if t.category_id is not None}  # ty: ignore[unresolved-attribute]
        | {p.category_id for p in proposals if p.category_id is not None}  # ty: ignore[unresolved-attribute]
    )
```

And in the result loop build the field:

```python
        proposal = by_txn_proposal.get(t.id)
        proposal_out = None
        if proposal is not None:
            pcat = cats.get(proposal.category_id) if proposal.category_id else None  # ty: ignore[unresolved-attribute]
            proposal_out = ProposalOut(
                category=CategoryRef(id=pcat.id, name=pcat.name) if pcat else None,
                tags=tags_by_proposal.get(proposal.id, []),
                display_name=proposal.proposed_display_name,
                provenance=proposal.provenance,
            )
```

pass `proposal=proposal_out` to `TransactionOut(...)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_classification_api.py tests/test_transactions_api.py tests/test_rules_api.py -q`
Expected: PASS except the still-xfailed correction-log test (Task 9). `test_rules_api` proves the preview (which reuses the hydrator) tolerates the new field.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/transactions.py tests/test_classification_api.py tests/test_transactions_api.py
git commit -m "feat(api): inline pending proposal on TransactionOut (M5 CP3, #21)

Batch-hydrated (two added queries per page, no N+1); the inbox renders
from the list alone.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: GET /api/v1/correction-log

**Files:**
- Create: `src/pinch_backend/api/correction_log.py`
- Modify: `src/pinch_backend/api/app.py` (register router)
- Test: `tests/test_correction_log_api.py` (create); remove the last xfail in `tests/test_classification_api.py`

**Interfaces:**
- Produces: `GET /api/v1/correction-log` — `Page[CorrectionLogEntryOut]`, id-keyset, filters `transaction_id`, `actor`, `kind`; allowlist mirror of the wide columns.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_correction_log_api.py
"""The correction-log read surface (M5 CP3, #21): parity — eval export is
a consumer of this endpoint."""

import uuid

from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    User,
)

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email="taylor@example.com") -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert resp.status_code == 201, resp.text


async def _seed_entries(email="taylor@example.com") -> Ledger:
    user = await User.where(lambda u, e=email: u.email == e).first()
    member = (await user.memberships.all())[0]
    ledger = await Ledger.get(member.ledger_id)
    txn_id = uuid.uuid7()
    decision = await CorrectionLogEntry.create(
        ledger=ledger, transaction_id=txn_id,
        kind=CorrectionKind.DECISION, actor=CorrectionActor.USER,
        input_payee="starbucks", decision_tags=["treat"],
    )
    await CorrectionLogEntry.create(
        ledger=ledger, transaction_id=txn_id,
        kind=CorrectionKind.VOID, actor=CorrectionActor.USER,
        voids=decision.id, void_reason="import undone",
    )
    await CorrectionLogEntry.create(
        ledger=ledger, transaction_id=uuid.uuid7(),
        kind=CorrectionKind.DECISION, actor=CorrectionActor.AUTO,
    )
    return ledger


async def test_list_pages_and_filters(client, db) -> None:
    await _signup(client)
    ledger = await _seed_entries()

    everything = (await client.get("/api/v1/correction-log")).json()
    assert len(everything["items"]) == 3
    assert everything["next_cursor"] is None

    voids = (await client.get("/api/v1/correction-log?kind=void")).json()["items"]
    assert len(voids) == 1
    assert voids[0]["void_reason"] == "import undone"
    assert voids[0]["voids"] is not None

    autos = (await client.get("/api/v1/correction-log?actor=auto")).json()["items"]
    assert len(autos) == 1

    tid = everything["items"][0]["transaction_id"]
    scoped = (await client.get(f"/api/v1/correction-log?transaction_id={tid}")).json()["items"]
    assert all(e["transaction_id"] == tid for e in scoped)


async def test_log_is_ledger_scoped(client, db) -> None:
    await _signup(client)
    await _seed_entries()
    # A second user sees an empty log, not ours.
    from litestar.testing import AsyncTestClient

    from pinch_backend.api.app import create_app

    async with AsyncTestClient(
        create_app(manage_database=False), base_url="https://testserver.local"
    ) as other:
        await _signup(other, email="other@example.com")
        items = (await other.get("/api/v1/correction-log")).json()["items"]
        assert items == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_correction_log_api.py -x -q`
Expected: FAIL — 404 (no such route).

- [ ] **Step 3: Implement the router**

Create `src/pinch_backend/api/correction_log.py`:

```python
"""/api/v1/correction-log — the append-only decision record, readable
(PRD M5 #21): the parity principle applied; M9's eval export is a consumer
of this endpoint. Read-only — entries are written by consume/undo, never
over HTTP."""

import uuid
from datetime import date, datetime
from typing import Annotated

from litestar import Router, get
from litestar.di import NamedDependency
from litestar.params import QueryParameter
from pydantic import BaseModel

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    ProposalProvenance,
)


class CorrectionLogEntryOut(BaseModel):
    """One log entry — an allowlist mirror of the wide, self-contained row."""

    id: uuid.UUID
    transaction_id: uuid.UUID
    kind: CorrectionKind
    actor: CorrectionActor
    input_description_raw: str | None
    input_payee: str | None
    input_amount_minor: int | None
    input_currency: str | None
    input_date: date | None
    input_account_id: uuid.UUID | None
    proposal_category_id: uuid.UUID | None
    proposal_category_name: str | None
    proposal_tags: list[str]
    proposal_display_name: str | None
    proposal_provenance: ProposalProvenance | None
    proposal_detail: dict | None
    decision_category_id: uuid.UUID | None
    decision_category_name: str | None
    decision_tags: list[str]
    decision_display_name: str | None
    voids: uuid.UUID | None
    void_reason: str | None
    created_at: datetime


def _out(e: CorrectionLogEntry) -> CorrectionLogEntryOut:
    return CorrectionLogEntryOut(
        id=e.id,
        transaction_id=e.transaction_id,
        kind=e.kind,
        actor=e.actor,
        input_description_raw=e.input_description_raw,
        input_payee=e.input_payee,
        input_amount_minor=e.input_amount_minor,
        input_currency=e.input_currency,
        input_date=e.input_date,
        input_account_id=e.input_account_id,
        proposal_category_id=e.proposal_category_id,
        proposal_category_name=e.proposal_category_name,
        proposal_tags=e.proposal_tags,
        proposal_display_name=e.proposal_display_name,
        proposal_provenance=e.proposal_provenance,
        proposal_detail=e.proposal_detail,
        decision_category_id=e.decision_category_id,
        decision_category_name=e.decision_category_name,
        decision_tags=e.decision_tags,
        decision_display_name=e.decision_display_name,
        voids=e.voids,
        void_reason=e.void_reason,
        created_at=e.created_at,
    )


@get("/")
async def list_correction_log(
    current_ledger: NamedDependency[Ledger],
    transaction_id: Annotated[uuid.UUID | None, QueryParameter()] = None,
    actor: Annotated[CorrectionActor | None, QueryParameter()] = None,
    kind: Annotated[CorrectionKind | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[CorrectionLogEntryOut]:
    ledger_id = current_ledger.id
    query = CorrectionLogEntry.where(lambda e: e.ledger_id == ledger_id)
    if transaction_id is not None:
        tid = transaction_id
        query = query.where(lambda e, tid=tid: e.transaction_id == tid)
    if actor is not None:
        wanted_actor = actor
        query = query.where(lambda e, a=wanted_actor: e.actor == a)
    if kind is not None:
        wanted_kind = kind
        query = query.where(lambda e, k=wanted_kind: e.kind == k)
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    return Page(items=[_out(e) for e in rows], next_cursor=next_cursor)


correction_log_router = Router(path="/api/v1/correction-log", route_handlers=[list_correction_log])
```

Register in `src/pinch_backend/api/app.py`: import `correction_log_router` and add it to `route_handlers` (after `categories_router`).

Remove the remaining `@pytest.mark.xfail` in `tests/test_classification_api.py` (the auto-file test that reads `/correction-log`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_correction_log_api.py tests/test_classification_api.py tests/test_api_conventions.py -q`
Expected: PASS, no remaining xfails.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/correction_log.py src/pinch_backend/api/app.py \
        tests/test_correction_log_api.py tests/test_classification_api.py
git commit -m "feat(api): correction-log read surface with filters (M5 CP3, #21)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: Import undo — delete proposals, void log entries

**Files:**
- Modify: `src/pinch_backend/api/imports.py` (`delete_import`)
- Test: `tests/test_classification_api.py` (extend)

**Interfaces:**
- Consumes: models; the undo transaction in `delete_import`.
- Produces: undo deletes the import's transactions' Proposals (+ ProposalTags) and appends `kind=void` entries (actor=user, reason "import undone") for every not-yet-voided decision entry referencing those transaction ids — same atomic transaction. The M4 docstring contract, fulfilled.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_classification_api.py`:

```python
async def test_undo_voids_log_and_retracts_history(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    import_id = await _commit_csv(client, account_id, auto_file=True)
    await run_jobs()

    decisions = (await client.get(f"{LOG}?kind=decision")).json()["items"]
    assert decisions

    undone = await client.delete(f"{IMPORTS}/{import_id}", headers=await _csrf(client))
    assert undone.status_code == 204

    assert await _transactions(client) == []
    entries = (await client.get(LOG)).json()["items"]
    voids = [e for e in entries if e["kind"] == "void"]
    kept = [e for e in entries if e["kind"] == "decision"]
    assert len(kept) == len(decisions)  # voided, never deleted
    assert {v["voids"] for v in voids} == {d["id"] for d in decisions}
    assert all(v["void_reason"] == "import undone" for v in voids)
    assert all(v["actor"] == "user" for v in voids)

    # History no longer matches the retracted payee: a fresh import of the
    # same payee gets provenance=none, not history.
    await _commit_csv(client, account_id)
    await run_jobs()
    by_payee = {t["description_normalized"]: t for t in await _transactions(client)}
    assert by_payee["starbucks 123"]["proposal"]["provenance"] == "none"


async def test_repeated_undo_cycles_keep_one_void_per_decision(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    first = await _commit_csv(client, account_id, auto_file=True)
    await run_jobs()
    await client.delete(f"{IMPORTS}/{first}", headers=await _csrf(client))
    # Same file again: same payees, fresh transactions, fresh decisions.
    second = await _commit_csv(client, account_id, auto_file=True)
    await run_jobs()
    await client.delete(f"{IMPORTS}/{second}", headers=await _csrf(client))
    entries = (await client.get(LOG)).json()["items"]
    decisions = [e for e in entries if e["kind"] == "decision"]
    voids = [e for e in entries if e["kind"] == "void"]
    assert len(voids) == len(decisions)  # exactly one void per decision, ever
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_classification_api.py -x -q -k undo`
Expected: FAIL — no void entries appear (undo doesn't know the log yet).

- [ ] **Step 3: Extend delete_import**

In `src/pinch_backend/api/imports.py`, import `CorrectionActor, CorrectionKind, CorrectionLogEntry, Proposal, ProposalTag` from models. Replace the `delete_import` transaction block with:

```python
    async with transaction():
        txn_ids = [
            t.id
            for t in await Transaction.where(lambda t: t.source_import_id == batch_id).all()
        ]
        if txn_ids:
            # Retraction over CP3's tables (the M4 forward contract, bound
            # here): proposals die with their transactions; log entries are
            # voided with a later entry, never deleted.
            proposal_ids = [
                p.id
                for p in await Proposal.where(
                    lambda p, ids=txn_ids: p.transaction_id.in_(ids)
                ).all()
            ]
            if proposal_ids:
                await ProposalTag.where(
                    lambda pt, ids=proposal_ids: pt.proposal_id.in_(ids)
                ).delete()
                await Proposal.where(lambda p, ids=proposal_ids: p.id.in_(ids)).delete()
            decisions = await CorrectionLogEntry.where(
                lambda e, ids=txn_ids: (e.transaction_id.in_(ids))
                & (e.kind == CorrectionKind.DECISION)
            ).all()
            decision_ids = [d.id for d in decisions]
            already_voided = (
                {
                    v.voids
                    for v in await CorrectionLogEntry.where(
                        lambda v, ids=decision_ids: v.voids.in_(ids)
                    ).all()
                }
                if decision_ids
                else set()
            )
            for decision in decisions:
                if decision.id in already_voided:
                    continue
                await CorrectionLogEntry.create(
                    ledger=current_ledger,
                    transaction_id=decision.transaction_id,
                    kind=CorrectionKind.VOID,
                    actor=CorrectionActor.USER,
                    voids=decision.id,
                    void_reason="import undone",
                )
        await Transaction.where(lambda t: t.source_import_id == batch_id).delete()
        await ImportRow.where(lambda r: r.import_batch_id == batch_id).delete()
        await batch.delete()
```

Also update the `delete_import` docstring's forward-contract sentence — it is no longer forward: "…the correction log voids affected decisions with a later entry" now happens right here; reword to state it does.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_classification_api.py tests/test_imports_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/imports.py tests/test_classification_api.py
git commit -m "feat(api): import undo deletes proposals, voids log entries (M5 CP3, #21)

The M4 retraction contract, fulfilled: same atomic transaction, voided
never deleted, history stops matching the retracted payee.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 11: Category delete — re-point / empty pending proposals

**Files:**
- Modify: `src/pinch_backend/api/categories.py` (`delete_category`)
- Test: `tests/test_categories_api.py` (extend)

**Interfaces:**
- Produces: inside the existing delete transaction, pending proposals targeting the category are re-pointed (`category_id = reassign_to`) or, on a null disposition, emptied (`category_id=None, provenance=NONE, provenance_detail=None`); proposal tags/rename survive either way.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_categories_api.py`, reusing that file's existing `_signup`/`_csrf`-style helpers for the HTTP side (match their exact local names when writing the file):

```python
async def _seed_proposal_targeting(category_id: str):
    """A transaction + pending proposal aimed at ``category_id``. Model-layer
    on purpose: the pending proposal is pipeline-owned state and the surface
    under test is DELETE /categories."""
    import uuid as _uuid
    from datetime import date as _date

    from pinch_backend.models import (
        Account,
        AccountKind,
        Category,
        Ledger,
        Proposal,
        ProposalProvenance,
        Transaction,
    )

    ledger = (await Ledger.all())[0]
    account = await Account.create(ledger=ledger, kind=AccountKind.DEPOSITORY, label="Chk")
    txn = await Transaction.create(
        ledger=ledger, account=account, date=_date(2026, 7, 1), amount_minor=-100,
        currency="USD", description_raw="X", description_normalized="x",
        fingerprint=f"fp-{_uuid.uuid4().hex[:8]}",
    )
    target = await Category.get(_uuid.UUID(category_id))
    await Proposal.create(
        ledger=ledger, transaction=txn, category=target,
        provenance=ProposalProvenance.RULE, provenance_detail={"rule_ids": ["r"]},
    )
    return txn


async def test_delete_repoints_pending_proposals(client) -> None:
    from pinch_backend.models import Proposal, ProposalProvenance

    await _signup(client)
    a = (await client.post(
        "/api/v1/categories", json={"name": "Doomed Q"}, headers=await _csrf(client)
    )).json()
    b = (await client.post(
        "/api/v1/categories", json={"name": "Target Q"}, headers=await _csrf(client)
    )).json()
    txn = await _seed_proposal_targeting(a["id"])

    resp = await client.request(
        "DELETE",
        f"/api/v1/categories/{a['id']}",
        json={"reassign_to": b["id"]},
        headers=await _csrf(client),
    )
    assert resp.status_code == 204
    p = await Proposal.where(lambda p, tid=txn.id: p.transaction_id == tid).first()
    assert str(p.category_id) == b["id"]
    assert p.provenance is ProposalProvenance.RULE  # re-point keeps provenance


async def test_delete_with_null_disposition_empties_proposals(client) -> None:
    from pinch_backend.models import Proposal, ProposalProvenance

    await _signup(client)
    a = (await client.post(
        "/api/v1/categories", json={"name": "Doomed R"}, headers=await _csrf(client)
    )).json()
    txn = await _seed_proposal_targeting(a["id"])

    resp = await client.request(
        "DELETE",
        f"/api/v1/categories/{a['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert resp.status_code == 204
    p = await Proposal.where(lambda p, tid=txn.id: p.transaction_id == tid).first()
    assert p.category_id is None
    assert p.provenance is ProposalProvenance.NONE
    assert p.provenance_detail is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_categories_api.py -x -q -k proposal`
Expected: FAIL — the proposal still targets the deleted category id (or a FK cascade deleted the row; either way the assertions fail).

- [ ] **Step 3: Extend delete_category**

In `src/pinch_backend/api/categories.py`, import `Proposal, ProposalProvenance` from models. Inside the `async with transaction():` block, before `await category.delete()`:

```python
        # Pending proposals follow the disposition (PRD M5 D4): re-pointed at
        # the target, or emptied to provenance=none — the pipeline's decision
        # died with the category. Tags/rename survive; they were never the
        # category's decision. Must precede the delete (FK cascade).
        if target is not None:
            await Proposal.where(lambda p: p.category_id == cid).update(
                category_id=target.id, updated_at=utcnow()
            )
        else:
            await Proposal.where(lambda p: p.category_id == cid).update(
                category_id=None,
                provenance=ProposalProvenance.NONE,
                provenance_detail=None,
                updated_at=utcnow(),
            )
```

(import `utcnow` from models; the existing `Transaction.where(...).update(...)` line above it may also gain `updated_at=utcnow()` for consistency — do it, it's the same convention the imports CAS uses.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_categories_api.py -q`
Expected: PASS.

- [ ] **Step 5: Full-suite gate + commit**

Run: `uv run pytest -q` and `uv run prek run --all-files`
Expected: everything green.

```bash
git add src/pinch_backend/api/categories.py tests/test_categories_api.py
git commit -m "feat(api): category delete re-points or empties pending proposals (M5 CP3, #21)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Final verification (before the CP3 checkbox)

- `uv run pytest -q` — full suite green on Postgres (local-pg).
- `uv run prek run --all-files` — ruff, ty, docs hooks green.
- `grep -rn sqlite src tests .github README.md` — nothing left.
- Acceptance criteria of #21 traced: commit-never-classifies (T7), precedence matrix (T5), keyless no-reprocess (T5/T7), history semantics (T3/T5), auto-file (T5/T7), undo voids + history retraction (T10), unique guard under concurrency (T5), testing connector at the API seam (T6/T7).
- Push and tick the CP3 box on PR #23; update the PR body with a CP3 section.
