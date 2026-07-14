# M5 CP1 — Taxonomy, Tags, and Transaction User-Data Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the editable category taxonomy, free-form tags, the transaction user-data columns (category, display name, notes, reviewed state) plus the normalized payee, and the date-ordered transaction list/get/patch — the foundation every later M5 slice consumes.

**Architecture:** New `Category`, `Tag`, `TransactionTag` domain models plus additive columns on `Transaction`; a `taxonomy` module holding the seed set and depth-agnostic tree helpers; three new Litestar routers (`/api/v1/categories`, `/api/v1/tags`, `/api/v1/transactions`) following the M4 accounts pattern; a composite `(date desc, id desc)` keyset paginator alongside the existing id-keyset one. All domain access through ferro-orm; every handler reaches data via `current_ledger`.

**Tech Stack:** Python 3.14, Litestar, ferro-orm 0.16.1, pydantic v2, pytest (HTTP seam via `AsyncTestClient`, both sqlite + Postgres backends).

## Global Constraints

- **ferro-orm only** for all domain data access (ADR-0003, block-on-ferro). If a needed capability is missing, stop and file a ferro PRD — do not work around it.
- **Every handler reaches domain data via `current_ledger`** (AGENTS I-2), never by querying `Ledger`/membership directly.
- **Every list endpoint returns `Page[T]`**; tenancy misses answer **404, never a confirming 403**; responses are explicit allowlists, never the ORM row.
- **Writes are unsafe HTTP methods** so the M3 scope guard applies by construction; no handler re-checks write scope.
- **Money** is integer minor units + ISO 4217 (unchanged here; no new money fields).
- **Fail loudly, no half-measures** (AGENTS I-1). No "best effort".
- **Self-referential FK spelling** (ferro 0.16.1, scratch-verified): `parent: Annotated[Optional["Category"], ForeignKey(related_name="children")] = None`. The `"Category | None"` string and union-outside-`Annotated` spellings both fail.
- **Traversal joins are INNER**; uncategorized filtering uses `category_id == None` (lowers to `IS NULL`, no join) — never relation traversal that would drop NULL-FK rows.
- **Depth cap = 2, as validation only.** `MAX_DEPTH` is the sole place the number lives; all tree logic is walk-until-done.
- **No `description_normalized` backfill** — pre-deployment, no migration framework; the column is non-null, computed at write.
- **Commit style:** conventional commits, e.g. `feat(api): ...`, `feat(models): ...`; end the CP1-completing commit body referencing `(M5 CP1, #19)`. Co-author trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **Run tests on both backends** before each commit: sqlite (default) and Postgres (`PINCH_TEST_DATABASE_URL=postgres://postgres:password@localhost:5432/postgres`).

---

## Task 1: Enable ferro migration flags

**Files:**
- Modify: `src/pinch_backend/settings.py`
- Modify: `src/pinch_backend/db.py`
- Test: `tests/test_settings.py` (create)

**Interfaces:**
- Produces: `settings.database_migrate_updates: bool`, `settings.database_migrate_destructive: bool` (both default `True`); `connect_database()` passes them to `ferro.connect(migrate_updates=..., migrate_destructive=...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings.py
"""Settings that govern schema migration during active development (M5 CP1)."""

from pinch_backend.settings import Settings


def test_migration_flags_default_on_for_development() -> None:
    s = Settings()
    assert s.database_migrate_updates is True
    assert s.database_migrate_destructive is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings.py -v`
Expected: FAIL with `AttributeError` (fields do not exist yet).

- [ ] **Step 3: Add the settings fields**

In `src/pinch_backend/settings.py`, immediately after the `database_auto_migrate` field:

```python
    database_migrate_updates: bool = True
    """Let auto_migrate ALTER existing tables (add/modify columns) on connect.
    On in development — Pinch is pre-deployment and wipe-and-reset is free;
    disabled for hosted deploys once the schema stabilizes (ADR-0002 config)."""
    database_migrate_destructive: bool = True
    """Let auto_migrate DROP columns/tables that no longer exist in the models.
    On in development for the same reason; there are no users to lose."""
```

- [ ] **Step 4: Wire the flags into connect**

In `src/pinch_backend/db.py`, replace the body of `connect_database()`:

```python
async def connect_database() -> None:
    await ferro.connect(
        settings.database_url,
        auto_migrate=settings.database_auto_migrate,
        migrate_updates=settings.database_migrate_updates,
        migrate_destructive=settings.database_migrate_destructive,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pinch_backend/settings.py src/pinch_backend/db.py tests/test_settings.py
git commit -m "feat(db): enable ferro migrate_updates/destructive in development (M5 CP1, #19)"
```

---

## Task 2: Domain models — Category, Tag, TransactionTag, Transaction columns

**Files:**
- Modify: `src/pinch_backend/models.py`
- Test: `tests/test_taxonomy_models.py` (create)

**Interfaces:**
- Produces:
  - `Category(id, ledger, name, parent: Optional[Category], children, created_at, updated_at)` — self-referential nullable parent.
  - `Tag(id, ledger, name, name_fold, created_at, updated_at)` — `name_fold` casefolded, unique per `(ledger_id, name_fold)`.
  - `TransactionTag(id, ledger, transaction, tag, created_at, updated_at)` — unique per `(transaction_id, tag_id)`.
  - `Transaction` gains: `category: Optional[Category]` (nullable FK), `display_name: str | None`, `notes: str | None`, `reviewed_at: datetime | None`, `description_normalized: str` (non-null), indexed `(ledger_id, description_normalized)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_taxonomy_models.py
"""Model-layer invariants for the M5 CP1 tables (issue #19)."""

import pytest
from ferro import UniqueViolationError

from pinch_backend.models import (
    Category,
    Ledger,
    Tag,
    provision_user,
)


async def _ledger(db) -> Ledger:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    return (await Ledger.all())[0]


async def test_category_parent_is_a_nullable_self_reference(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)

    assert rest.parent_id == food.id
    children = await Category.where(lambda c: c.parent_id == food.id).all()
    assert [c.name for c in children] == ["Restaurants"]
    roots = await Category.where(lambda c: c.parent_id == None).all()  # noqa: E711
    assert food.id in {c.id for c in roots}


async def test_tag_fold_is_unique_per_ledger(db) -> None:
    ledger = await _ledger(db)
    await Tag.create(ledger=ledger, name="Vacation", name_fold="vacation")
    with pytest.raises(UniqueViolationError):
        await Tag.create(ledger=ledger, name="vacation", name_fold="vacation")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_taxonomy_models.py -v`
Expected: FAIL with `ImportError`/`AttributeError` (`Category`, `Tag` not defined).

- [ ] **Step 3: Add the imports and models**

In `src/pinch_backend/models.py`, add `Optional` to typing imports:

```python
from typing import TYPE_CHECKING, Annotated, ClassVar, Optional
```

Add the new models after the `Transaction` class (order does not matter to ferro, but keep classification tables grouped). Insert **before** the `provision_user` function:

```python
class Category(TimestampMixin, Model):
    """A node in the ledger's editable classification taxonomy (PRD M5 #19).

    A transaction has at most one category and may be uncategorized (a NULL
    FK — the pipeline's bottom case and a legitimate reviewed state). Nesting
    is a plain self-referential parent FK; the two-level depth cap is an API
    validation (pinch_backend.taxonomy), never encoded here — the schema
    stays depth-agnostic so raising the cap is one constant.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="categories", index=True)]
    name: str
    parent: Annotated[Optional["Category"], ForeignKey(related_name="children")] = None
    """The verified ferro 0.16.1 self-FK spelling. NULL = a top-level node."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    children: Relation[list["Category"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()


class Tag(TimestampMixin, Model):
    """A free-form, optional label; a transaction may carry many (CONTEXT.md).

    Created implicitly on first use. ``name_fold`` is the casefolded name and
    the uniqueness key, so "Vacation" and "vacation" never fork; the original
    casing is preserved in ``name`` for display.
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("ledger_id", "name_fold"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="tags", index=True)]
    name: str
    name_fold: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    transaction_tags: Relation[list["TransactionTag"]] = BackRef()


class TransactionTag(TimestampMixin, Model):
    """The transaction↔tag join (CONTEXT.md: a transaction carries many tags).

    Deleting a tag detaches it everywhere by removing these rows; tags are
    never load-bearing, so no reassignment machinery.
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("transaction_id", "tag_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="transaction_tags", index=True)]
    """The tenancy column (ADR-0002), denormalized so row-level security has
    one ownership column on every domain table."""
    transaction: Annotated["Transaction", ForeignKey(related_name="transaction_tags", index=True)]
    tag: Annotated[Tag, ForeignKey(related_name="transaction_tags", index=True)]
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
```

Update the `Ledger` back-references (add to the `Ledger` class body, alongside the existing `transactions` BackRef):

```python
    categories: Relation[list["Category"]] = BackRef()
    tags: Relation[list["Tag"]] = BackRef()
    transaction_tags: Relation[list["TransactionTag"]] = BackRef()
```

Add the user-data and payee columns to `Transaction` (after `fingerprint`, before the timestamps), and extend its composite indexes:

```python
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("ledger_id", "date"),
        ("account_id", "fingerprint"),
        ("ledger_id", "description_normalized"),
    )
```

```python
    description_normalized: str
    """The **payee** (CONTEXT.md): NFKC → casefold → collapse whitespace →
    trim of description_raw, via imports.fingerprint.normalize_description.
    Source data, computed at write, indexed per-ledger for CP3 history
    matching. Non-null — first deploy runs on an empty schema, so no backfill."""
    category: Annotated[Optional["Category"], ForeignKey(related_name="transactions")] = None
    """User data (M5): the assigned category, or NULL for uncategorized."""
    display_name: str | None = None
    """User data: an override of description_raw for display; NULL shows the
    raw description (an override, never a copy — source rewrites shine through)."""
    notes: str | None = None
    """User data: free-form user annotation."""
    reviewed_at: datetime | None = None
    """User data: when the user cleared this from the review inbox; NULL means
    still in the inbox. M7 reopens review by nulling it."""
```

Add the tag back-reference to `Transaction` (alongside its other relations):

```python
    transaction_tags: Relation[list["TransactionTag"]] = BackRef()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_taxonomy_models.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing suite to confirm the additive columns didn't break M4**

Run: `uv run pytest tests/test_domain.py tests/test_imports_api.py -q`
Expected: PASS (the new Transaction columns are nullable or set by Task 3).

Note: `description_normalized` is non-null, so any code path that creates a `Transaction` must set it. The only such path today is the import commit — Task 3 fixes it. If `test_imports_api.py` fails here with a not-null violation, that is expected until Task 3; proceed to Task 3 and re-run.

- [ ] **Step 6: Commit**

```bash
git add src/pinch_backend/models.py tests/test_taxonomy_models.py
git commit -m "feat(models): Category, Tag, TransactionTag, and transaction user-data columns (M5 CP1, #19)"
```

---

## Task 3: Populate `description_normalized` on import commit

**Files:**
- Modify: `src/pinch_backend/api/imports.py` (the `commit_import` transaction-construction block)
- Test: `tests/test_imports_api.py` (add one assertion)

**Interfaces:**
- Consumes: `normalize_description` from `pinch_backend.imports.fingerprint`.
- Produces: every imported `Transaction` carries a correct `description_normalized`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_imports_api.py`:

```python
async def test_committed_transactions_carry_normalized_payee(client) -> None:
    from pinch_backend.imports.fingerprint import normalize_description
    from pinch_backend.models import Transaction

    account_id = await _import_and_commit_one(client, description="  COSTCO   WHSE  #42 ")
    txn = await Transaction.where(lambda t: t.account_id == account_id).first()
    assert txn.description_normalized == normalize_description("  COSTCO   WHSE  #42 ")
    assert txn.description_normalized == "costco whse #42"
```

If `_import_and_commit_one` does not already exist in the test module, add this helper (drives the real M4 seam: upload → confirm mapping → commit):

```python
async def _import_and_commit_one(client, *, description: str) -> str:
    account = await _create_account(client)
    csv_body = f"date,amount,description\n2026-01-15,-9.99,{description}\n"
    upload = await client.post(
        "/api/v1/imports",
        files={"file": ("bank.csv", csv_body, "text/csv")},
        data={"account_id": account["id"]},
        headers=await _csrf(client),
    )
    assert upload.status_code == 201, upload.text
    import_id = upload.json()["id"]
    mapping = upload.json()["suggested_mapping"]
    await client.post(
        f"/api/v1/imports/{import_id}/mapping", json=mapping, headers=await _csrf(client)
    )
    commit = await client.post(
        f"/api/v1/imports/{import_id}/commit", json={}, headers=await _csrf(client)
    )
    assert commit.status_code == 200, commit.text
    return account["id"]
```

(Reuse `_create_account`, `_csrf`, `_signup` conventions already in the test suite; add a `_signup` call in a fixture or at the top of the test if the module does not already sign in.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_imports_api.py::test_committed_transactions_carry_normalized_payee -v`
Expected: FAIL — either a not-null violation on `description_normalized`, or `AttributeError`.

- [ ] **Step 3: Set the column in the commit loop**

In `src/pinch_backend/api/imports.py`, add the import near the other fingerprint import:

```python
from pinch_backend.imports.fingerprint import compute_fingerprint, normalize_description
```

In `commit_import`, inside the `Transaction(...)` list comprehension, add the field (right after `description_raw=...`):

```python
                description_normalized=normalize_description(row.description_raw or ""),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_imports_api.py -q`
Expected: PASS (the new assertion and all prior import tests).

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/imports.py tests/test_imports_api.py
git commit -m "feat(imports): compute description_normalized on committed transactions (M5 CP1, #19)"
```

---

## Task 4: Taxonomy tree helpers

**Files:**
- Create: `src/pinch_backend/taxonomy.py`
- Test: `tests/test_taxonomy_helpers.py` (create)

**Interfaces:**
- Produces (pure/async helpers over ferro):
  - `MAX_DEPTH: int = 2`
  - `async def category_depth(category: Category) -> int` — 1 for a root, 2 for its child, … (walk-to-root).
  - `async def validate_placement(ledger_id, parent: Category | None) -> None` — raise `ClientException` (400) if adding a child under `parent` would exceed `MAX_DEPTH`.
  - `async def check_no_cycle(category: Category, new_parent: Category | None) -> None` — raise `ClientException` (400) if `category` appears in `new_parent`'s ancestry (or is `new_parent`).
  - `async def collect_descendant_ids(root_ids: list[uuid.UUID], ledger_id: uuid.UUID) -> set[uuid.UUID]` — the closure of `root_ids` plus all descendants, walk-until-done.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_taxonomy_helpers.py
"""Depth-agnostic tree helpers for the category taxonomy (M5 CP1, #19)."""

import pytest
from litestar.exceptions import ClientException

from pinch_backend import taxonomy
from pinch_backend.models import Category, Ledger, provision_user


async def _ledger(db) -> Ledger:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    return (await Ledger.all())[0]


async def test_depth_counts_from_root(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    assert await taxonomy.category_depth(food) == 1
    assert await taxonomy.category_depth(rest) == 2


async def test_placement_under_a_depth_2_node_is_rejected(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    with pytest.raises(ClientException):
        await taxonomy.validate_placement(ledger.id, rest)


async def test_cycle_is_rejected(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    # Re-parenting Food under its own descendant Restaurants is a cycle.
    with pytest.raises(ClientException):
        await taxonomy.check_no_cycle(food, rest)


async def test_collect_descendants_is_the_closure(db) -> None:
    ledger = await _ledger(db)
    food = await Category.create(ledger=ledger, name="Food")
    rest = await Category.create(ledger=ledger, name="Restaurants", parent=food)
    coffee = await Category.create(ledger=ledger, name="Coffee", parent=food)
    other = await Category.create(ledger=ledger, name="Travel")
    ids = await taxonomy.collect_descendant_ids([food.id], ledger.id)
    assert ids == {food.id, rest.id, coffee.id}
    assert other.id not in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_taxonomy_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: pinch_backend.taxonomy`.

- [ ] **Step 3: Write the module (helpers only; seed added in Task 5)**

```python
# src/pinch_backend/taxonomy.py
"""The category taxonomy: the seeded starter set and depth-agnostic tree
helpers (PRD M5, issue #19).

The two-level depth cap lives in exactly one constant, ``MAX_DEPTH``. Every
helper walks until done rather than assuming a depth, so raising the cap is a
one-line change and nothing else in the system knows the number.
"""

import uuid

from litestar.exceptions import ClientException

from pinch_backend.models import Category, Ledger

MAX_DEPTH = 2
"""Top-level groups plus one child level (CONTEXT.md: Food → Restaurants).
The only place the depth is written down."""


async def category_depth(category: Category) -> int:
    """1 for a root, 2 for its child, … — walk to the root, counting hops."""
    depth = 1
    current = category
    while current.parent_id is not None:
        parent = await Category.get(current.parent_id)
        depth += 1
        current = parent
    return depth


async def validate_placement(ledger_id: uuid.UUID, parent: Category | None) -> None:
    """Reject (400) a child placed under ``parent`` if it would exceed the cap.
    A root (parent None) is always depth 1 and always allowed."""
    if parent is None:
        return
    if await category_depth(parent) >= MAX_DEPTH:
        raise ClientException(
            detail=f"Categories may nest at most {MAX_DEPTH} levels deep"
        )


async def check_no_cycle(category: Category, new_parent: Category | None) -> None:
    """Reject (400) re-parenting ``category`` under itself or a descendant."""
    current = new_parent
    while current is not None:
        if current.id == category.id:
            raise ClientException(detail="A category cannot be its own ancestor")
        current = await Category.get(current.parent_id) if current.parent_id else None


async def collect_descendant_ids(
    root_ids: list[uuid.UUID], ledger_id: uuid.UUID
) -> set[uuid.UUID]:
    """The closure of ``root_ids`` and all their descendants within the ledger.
    One query loads the ledger's categories (tiny); the walk is in memory."""
    cats = await Category.where(lambda c: c.ledger_id == ledger_id).all()
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for c in cats:
        if c.parent_id is not None:
            children.setdefault(c.parent_id, []).append(c.id)
    result: set[uuid.UUID] = set()
    stack = list(root_ids)
    while stack:
        node = stack.pop()
        if node in result:
            continue
        result.add(node)
        stack.extend(children.get(node, ()))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_taxonomy_helpers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/taxonomy.py tests/test_taxonomy_helpers.py
git commit -m "feat(taxonomy): depth-agnostic tree helpers with a single depth constant (M5 CP1, #19)"
```

---

## Task 5: Seed the default taxonomy on ledger provisioning

**Files:**
- Modify: `src/pinch_backend/taxonomy.py` (add `seed_default_taxonomy`)
- Modify: `src/pinch_backend/models.py` (`provision_user` calls the seeder)
- Test: `tests/test_taxonomy_seed.py` (create)

**Interfaces:**
- Consumes: `Category`, `Ledger`.
- Produces: `async def seed_default_taxonomy(ledger: Ledger) -> None` — inserts the starter set (12 top-level, 28 children) via two `bulk_create` calls; called inside `provision_user`'s existing transaction.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_taxonomy_seed.py
"""Every new ledger is born with the starter taxonomy (M5 CP1, #19)."""

from pinch_backend.models import Category, Ledger, provision_user
from pinch_backend.taxonomy import DEFAULT_TAXONOMY


async def test_provisioning_seeds_the_default_taxonomy(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]

    cats = await Category.where(lambda c: c.ledger_id == ledger.id).all()
    top = [c for c in cats if c.parent_id is None]
    children = [c for c in cats if c.parent_id is not None]

    assert len(top) == len(DEFAULT_TAXONOMY)
    assert len(children) == sum(len(kids) for _, kids in DEFAULT_TAXONOMY)
    # Seeds are ordinary rows: every child points at a seeded top-level parent.
    top_ids = {c.id for c in top}
    assert all(c.parent_id in top_ids for c in children)


async def test_seed_is_deletable_like_any_row(db) -> None:
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    a_child = await Category.where(
        lambda c: (c.ledger_id == ledger.id) & (c.parent_id != None)  # noqa: E711
    ).first()
    await a_child.delete()  # no special-casing; nothing raises
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_taxonomy_seed.py -v`
Expected: FAIL — `ImportError` (`DEFAULT_TAXONOMY`) / provisioning does not seed.

- [ ] **Step 3: Add the seed data and seeder to `taxonomy.py`**

Add near the top of `src/pinch_backend/taxonomy.py` (after `MAX_DEPTH`):

```python
DEFAULT_TAXONOMY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Income", ("Paycheck", "Interest", "Other Income")),
    ("Housing", ("Rent & Mortgage", "Utilities", "Home Improvement")),
    ("Food & Drink", ("Groceries", "Restaurants", "Coffee")),
    ("Transportation", ("Gas", "Parking & Tolls", "Public Transit", "Auto & Ride Share")),
    ("Shopping", ("Clothing", "Electronics", "Household")),
    ("Health", ("Medical", "Pharmacy", "Fitness")),
    ("Entertainment", ("Streaming", "Events", "Hobbies")),
    ("Travel", ("Flights", "Lodging", "Rideshare")),
    ("Bills & Subscriptions", ("Phone", "Internet", "Software")),
    ("Personal Care", ()),
    ("Gifts & Donations", ()),
    ("Fees & Charges", ()),
)
"""The starter set seeded into every new ledger (12 top-level, 28 children).
Ordinary editable rows — the user may rename, re-parent, or delete any of
them; nothing in the pipeline assumes a category exists."""
```

Add the seeder function (uses the `_id` constructor form, matching the M4 `bulk_create` precedent; parents are instantiated first so their app-generated uuid7 ids are available for the children):

```python
async def seed_default_taxonomy(ledger: Ledger) -> None:
    """Insert the starter taxonomy for a freshly provisioned ledger. Runs
    inside provision_user's transaction — the ledger exists or none of this
    does. Two bulk inserts: parents (for their ids), then children."""
    parents = [
        Category(ledger_id=ledger.id, name=name)  # ty: ignore[unknown-argument]
        for name, _ in DEFAULT_TAXONOMY
    ]
    await Category.bulk_create(parents)
    children = [
        Category(  # ty: ignore[unknown-argument]
            ledger_id=ledger.id, name=child_name, parent_id=parent.id
        )
        for parent, (_, child_names) in zip(parents, DEFAULT_TAXONOMY, strict=True)
        for child_name in child_names
    ]
    if children:
        await Category.bulk_create(children)
```

- [ ] **Step 4: Call the seeder from `provision_user`**

In `src/pinch_backend/models.py`, add the import at the point of use to avoid a circular import (taxonomy imports models):

```python
async def provision_user(
    *,
    email: str,
    display_name: str,
    primary_currency: str = "USD",
    password_hash: str | None = None,
) -> User:
    from pinch_backend.taxonomy import seed_default_taxonomy

    async with transaction():
        ledger = await Ledger.create(name=display_name)
        user = await User.create(
            email=email,
            display_name=display_name,
            primary_currency=primary_currency,
            password_hash=password_hash,
        )
        await LedgerMember.create(user=user, ledger=ledger, role=LedgerRole.OWNER)
        await seed_default_taxonomy(ledger)
    return user
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_taxonomy_seed.py tests/test_domain.py -v`
Expected: PASS (provisioning atomicity tests in `test_domain.py` still hold — seeding is inside the same transaction).

- [ ] **Step 6: Commit**

```bash
git add src/pinch_backend/taxonomy.py src/pinch_backend/models.py tests/test_taxonomy_seed.py
git commit -m "feat(taxonomy): seed the starter taxonomy on ledger provisioning (M5 CP1, #19)"
```

---

## Task 6: Composite `(date, id)` keyset paginator

**Files:**
- Modify: `src/pinch_backend/api/pagination.py`
- Test: `tests/test_pagination_composite.py` (create)

**Interfaces:**
- Consumes: the `Page[T]` envelope and `ClientException` already in the module.
- Produces:
  - `def encode_date_cursor(txn_date: date, row_id: uuid.UUID) -> str`
  - `def decode_date_cursor(cursor: str) -> tuple[date, uuid.UUID]` — 400 on garbage.
  - `async def paginate_by_date(query, *, cursor, limit) -> tuple[list, str | None]` — orders `date desc, id desc`, keyset predicate `(date < d) | ((date == d) & (id < i))`.
- Requires each paginated row to expose `.date` and `.id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pagination_composite.py
"""The composite (date desc, id desc) keyset paginator (M5 CP1, #19)."""

import uuid
from datetime import date

import pytest
from litestar.exceptions import ClientException

from pinch_backend.api.pagination import decode_date_cursor, encode_date_cursor


def test_cursor_round_trips() -> None:
    d, i = date(2026, 1, 30), uuid.uuid7()
    assert decode_date_cursor(encode_date_cursor(d, i)) == (d, i)


def test_garbage_cursor_is_a_client_error() -> None:
    with pytest.raises(ClientException):
        decode_date_cursor("not-a-cursor")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pagination_composite.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Extend `pagination.py`**

Add imports at the top of `src/pinch_backend/api/pagination.py`:

```python
import base64
from datetime import date
```

Update the module docstring's final paragraph to note the composite variant (append):

```
A second variant, ``paginate_by_date``, keysets on ``(date desc, id desc)``
for the transaction list (M5): same Page[T] envelope and opaque cursor, no
OFFSET; the cursor carries the last row's date and id.
```

Append to the module:

```python
def encode_date_cursor(txn_date: date, row_id: uuid.UUID) -> str:
    """Opaque position for the (date, id) keyset: base64url of
    ``<iso-date>|<uuid>``. Opaque means clients pass it back verbatim and
    never parse it."""
    raw = f"{txn_date.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_date_cursor(cursor: str) -> tuple[date, uuid.UUID]:
    """Reverse of encode_date_cursor; anything else is a 400. The detail never
    echoes the value (request inputs are not reflected into responses)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        iso, _, id_str = raw.partition("|")
        return date.fromisoformat(iso), uuid.UUID(id_str)
    except (ValueError, UnicodeDecodeError):
        raise ClientException(detail="Invalid cursor") from None


class HasDateAndId(Protocol):
    """What a row must offer for date-keyset pagination."""

    id: uuid.UUID
    date: date


async def paginate_by_date[ModelT: HasDateAndId](
    query: "Query[ModelT]", *, cursor: str | None, limit: int
) -> tuple[list[ModelT], str | None]:
    """One keyset page ordered newest-first: ``date`` desc, ``id`` desc as the
    tiebreak, ``limit`` rows plus one probe row to learn if a next page
    exists without a COUNT."""
    if cursor is not None:
        after_date, after_id = decode_date_cursor(cursor)
        query = query.where(
            lambda row: (row.date < after_date)
            | ((row.date == after_date) & (row.id < after_id))
        )
    rows = (
        await query.order_by(lambda row: row.date, "desc")
        .order_by(lambda row: row.id, "desc")
        .limit(limit + 1)
        .all()
    )
    if len(rows) > limit:
        last = rows[limit - 1]
        return rows[:limit], encode_date_cursor(last.date, last.id)
    return rows, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pagination_composite.py -v`
Expected: PASS. (The `paginate_by_date` behavior is exercised end-to-end in Task 9.)

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/pagination.py tests/test_pagination_composite.py
git commit -m "feat(api): composite (date, id) keyset paginator for the transaction list (M5 CP1, #19)"
```

---

## Task 7: Categories API

> **Correction (post-implementation, shipped in commit 7fc5556):** the
> re-parent code below has two bugs found in review. (1) `category.parent =
> new_parent` must be `category.parent_id = new_parent.id if new_parent else
> None` — `parent` is a ferro relation ClassVar and is not settable on an
> instance. (2) The re-parent depth check must account for the *moved
> subtree's height*, not just the new parent's depth, or moving a
> node-with-children under a root pushes a grandchild past the depth-2 cap
> (D3); use a `taxonomy.subtree_height(category)` helper and check
> `new_node_depth + subtree_height(category) - 1 <= MAX_DEPTH`. (3) The
> DELETE reassign+delete must be wrapped in `async with transaction():`.
> A successful-re-parent test and a subtree-cap-rejection test were added.
> The shipped code is authoritative; the snippets below are left as the
> original plan for the record.

**Files:**
- Create: `src/pinch_backend/api/categories.py`
- Modify: `src/pinch_backend/api/app.py` (register `categories_router`)
- Test: `tests/test_categories_api.py` (create)

**Interfaces:**
- Consumes: `current_ledger`, `Page`, `paginate`, `taxonomy` helpers, `Category`, `Transaction`.
- Produces: router at `/api/v1/categories` with `POST /`, `GET /`, `GET /{id}`, `PATCH /{id}`, `DELETE /{id}` (body `{"reassign_to": uuid | null}`). `CategoryOut = {id, name, parent_id, created_at}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_categories_api.py
"""/api/v1/categories over the public seam (M5 CP1, #19)."""

CATEGORIES = "/api/v1/categories"
PASSWORD = "correct horse battery staple"


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


async def _create(client, name: str, parent_id: str | None = None):
    r = await client.post(
        CATEGORIES,
        json={"name": name, "parent_id": parent_id},
        headers=await _csrf(client),
    )
    return r


async def test_signup_seeds_a_listable_taxonomy(client) -> None:
    await _signup(client)
    r = await client.get(f"{CATEGORIES}?limit=100")
    assert r.status_code == 200
    names = {c["name"] for c in r.json()["items"]}
    assert {"Food & Drink", "Groceries", "Income"} <= names


async def test_depth_three_is_rejected(client) -> None:
    await _signup(client)
    food = (await _create(client, "MyFood")).json()
    sub = (await _create(client, "MySub", parent_id=food["id"])).json()
    r = await _create(client, "TooDeep", parent_id=sub["id"])
    assert r.status_code == 400


async def test_reparent_into_a_cycle_is_rejected(client) -> None:
    await _signup(client)
    a = (await _create(client, "A")).json()
    b = (await _create(client, "B", parent_id=a["id"])).json()
    r = await client.patch(
        f"{CATEGORIES}/{a['id']}", json={"parent_id": b["id"]}, headers=await _csrf(client)
    )
    assert r.status_code == 400


async def test_delete_requires_a_disposition_and_reassigns(client) -> None:
    await _signup(client)
    src = (await _create(client, "Src")).json()
    dst = (await _create(client, "Dst")).json()
    # A leaf with no children deletes with an explicit reassign target.
    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{src['id']}",
        json={"reassign_to": dst["id"]},
        headers=await _csrf(client),
    )
    assert r.status_code == 204, r.text


async def test_delete_is_blocked_by_children(client) -> None:
    await _signup(client)
    parent = (await _create(client, "Parent")).json()
    await _create(client, "Child", parent_id=parent["id"])
    r = await client.request(
        "DELETE",
        f"{CATEGORIES}/{parent['id']}",
        json={"reassign_to": None},
        headers=await _csrf(client),
    )
    assert r.status_code == 409


async def test_other_ledger_category_is_a_404(client) -> None:
    await _signup(client, "a@example.com")
    mine = (await _create(client, "Mine")).json()
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, "b@example.com")
    r = await client.get(f"{CATEGORIES}/{mine['id']}")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_categories_api.py -v`
Expected: FAIL — 404 for all routes (router not mounted).

- [ ] **Step 3: Write the router**

```python
# src/pinch_backend/api/categories.py
"""/api/v1/categories — the editable classification taxonomy (PRD M5 #19).

Same conventions as every domain surface: current_ledger (I-2), Page[T]
lists, allowlist responses, tenancy 404s, and the scope guard by
construction on every unsafe method. The two-level depth cap and cycle
prevention live in pinch_backend.taxonomy; nothing here hardcodes a depth.
"""

import uuid
from datetime import datetime

from litestar import Router, delete, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException, NotFoundException
from litestar.params import FromPath
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend import taxonomy
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import Category, Ledger, Transaction
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class CategoryCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str = Field(min_length=1, max_length=100)
    parent_id: uuid.UUID | None = None
    """A top-level node when null; otherwise nests under the named parent
    (depth-capped, validated server-side)."""


class CategoryUpdateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    parent_id: uuid.UUID | None = None
    """Re-parent target. Present-and-null moves the node to top level;
    absent leaves the parent unchanged (see reparent field)."""
    reparent: bool = False
    """True to apply parent_id (including null → top level). Distinguishes
    "move to top level" from "don't touch the parent" without a sentinel."""


class CategoryDeleteIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    reassign_to: uuid.UUID | None
    """Where this category's transactions go: another category, or null to
    make them uncategorized. Required — no default — because silently
    uncategorizing a year of history is exactly what we refuse to do (I-1)."""


class CategoryOut(BaseModel):
    """What a client may see about a category — an allowlist, never the row."""

    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    created_at: datetime


def _out(c: Category) -> CategoryOut:
    return CategoryOut(
        id=c.id, name=c.name, parent_id=c.parent_id, created_at=c.created_at
    )


async def _get(ledger: Ledger, category_id: uuid.UUID) -> Category:
    c = await Category.where(
        lambda x: (x.id == category_id) & (x.ledger_id == ledger.id)
    ).first()
    if c is None:
        raise NotFoundException(detail="No such category")
    return c


async def _assert_sibling_name_free(
    ledger_id: uuid.UUID, parent_id: uuid.UUID | None, name: str, exclude: uuid.UUID | None
) -> None:
    """Sibling names are unique (works for null and non-null parents, which a
    DB unique on a nullable column cannot guarantee alone)."""
    siblings = await Category.where(
        lambda c: (c.ledger_id == ledger_id) & (c.parent_id == parent_id)
    ).all()
    if any(s.name == name and s.id != exclude for s in siblings):
        raise ClientException(detail="A sibling category already has that name")


@post("/")
async def create_category(
    data: CategoryCreateIn, current_ledger: NamedDependency[Ledger]
) -> CategoryOut:
    parent = await _get(current_ledger, data.parent_id) if data.parent_id else None
    await taxonomy.validate_placement(current_ledger.id, parent)
    await _assert_sibling_name_free(current_ledger.id, data.parent_id, data.name, None)
    category = await Category.create(
        ledger=current_ledger, name=data.name, parent=parent
    )
    log.info("category.created", category_id=str(category.id), ledger_id=str(current_ledger.id))
    return _out(category)


@get("/")
async def list_categories(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[CategoryOut]:
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        Category.where(lambda c: c.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(items=[_out(c) for c in rows], next_cursor=next_cursor)


@get("/{category_id:uuid}")
async def get_category(
    category_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> CategoryOut:
    return _out(await _get(current_ledger, category_id))


@patch("/{category_id:uuid}")
async def update_category(
    category_id: FromPath[uuid.UUID],
    data: CategoryUpdateIn,
    current_ledger: NamedDependency[Ledger],
) -> CategoryOut:
    category = await _get(current_ledger, category_id)
    new_parent_id = category.parent_id
    if data.reparent:
        new_parent = await _get(current_ledger, data.parent_id) if data.parent_id else None
        await taxonomy.check_no_cycle(category, new_parent)
        await taxonomy.validate_placement(current_ledger.id, new_parent)
        category.parent = new_parent
        new_parent_id = data.parent_id
    if data.name is not None:
        category.name = data.name
    await _assert_sibling_name_free(
        current_ledger.id, new_parent_id, category.name, category.id
    )
    await category.save()
    log.info("category.updated", category_id=str(category.id), ledger_id=str(current_ledger.id))
    return _out(category)


@delete("/{category_id:uuid}")
async def delete_category(
    category_id: FromPath[uuid.UUID],
    data: CategoryDeleteIn,
    current_ledger: NamedDependency[Ledger],
) -> None:
    """Hard delete with an explicit disposition (CONTEXT.md / D4). Children
    block; rules-block arrives in CP2 and proposal re-point in CP3 — both add
    a guard here without changing this contract."""
    category = await _get(current_ledger, category_id)
    child = await Category.where(lambda c: c.parent_id == category_id).first()
    if child is not None:
        raise ClientException(
            detail="Move or delete this category's children first", status_code=409
        )
    target: Category | None = None
    if data.reassign_to is not None:
        target = await _get(current_ledger, data.reassign_to)
    cid = category.id
    await Transaction.where(lambda t: t.category_id == cid).update(
        category_id=target.id if target else None
    )
    await category.delete()
    log.info(
        "category.deleted",
        category_id=str(cid),
        ledger_id=str(current_ledger.id),
        reassigned_to=str(target.id) if target else None,
    )


categories_router = Router(
    path="/api/v1/categories",
    route_handlers=[
        create_category,
        list_categories,
        get_category,
        update_category,
        delete_category,
    ],
)
```

Note: `ClientException(detail=..., status_code=409)` is verified to accept `status_code` (Litestar's `ClientException` forwards it), so the children-block 409 above is correct as written.

- [ ] **Step 4: Register the router**

In `src/pinch_backend/api/app.py`, import and mount:

```python
from pinch_backend.api.categories import categories_router
```

Add `categories_router` to the `route_handlers` list in `create_app`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_categories_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pinch_backend/api/categories.py src/pinch_backend/api/app.py tests/test_categories_api.py
git commit -m "feat(api): categories CRUD with depth/cycle validation and delete-with-reassignment (M5 CP1, #19)"
```

---

## Task 8: Tags API

**Files:**
- Create: `src/pinch_backend/api/tags.py`
- Create: `src/pinch_backend/tags.py` (the shared `resolve_tags` helper, reused by the transaction PATCH in Task 10)
- Modify: `src/pinch_backend/api/app.py` (register `tags_router`)
- Test: `tests/test_tags_api.py` (create)

**Interfaces:**
- Produces:
  - `async def resolve_tags(ledger: Ledger, names: list[str]) -> list[Tag]` in `pinch_backend/tags.py` — implicit-create by casefold, returns the tag rows (deduped, order-preserving).
  - Router `/api/v1/tags`: `POST /` (explicit create), `GET /`, `DELETE /{id}` (detach + delete). `TagOut = {id, name, created_at}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tags_api.py
"""/api/v1/tags over the public seam (M5 CP1, #19)."""

TAGS = "/api/v1/tags"
PASSWORD = "correct horse battery staple"


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


async def test_create_and_list_tag(client) -> None:
    await _signup(client)
    r = await client.post(TAGS, json={"name": "Vacation-2026"}, headers=await _csrf(client))
    assert r.status_code == 201, r.text
    listing = await client.get(TAGS)
    assert "Vacation-2026" in {t["name"] for t in listing.json()["items"]}


async def test_casefold_collision_is_rejected(client) -> None:
    await _signup(client)
    await client.post(TAGS, json={"name": "Vacation"}, headers=await _csrf(client))
    r = await client.post(TAGS, json={"name": "vacation"}, headers=await _csrf(client))
    assert r.status_code == 409


async def test_delete_removes_the_tag(client) -> None:
    await _signup(client)
    created = await client.post(TAGS, json={"name": "temp"}, headers=await _csrf(client))
    tag_id = created.json()["id"]
    r = await client.request("DELETE", f"{TAGS}/{tag_id}", headers=await _csrf(client))
    assert r.status_code == 204
    listing = await client.get(TAGS)
    assert tag_id not in {t["id"] for t in listing.json()["items"]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tags_api.py -v`
Expected: FAIL — routes 404.

- [ ] **Step 3: Write the shared resolver**

```python
# src/pinch_backend/tags.py
"""Tag resolution shared by the tags API and the transaction PATCH (M5 CP1).

Tags are free-form and created implicitly on first use; ``name_fold`` (the
casefolded name) is the identity, so "Vacation" and "vacation" never fork.
"""

from pinch_backend.models import Ledger, Tag


async def resolve_tags(ledger: Ledger, names: list[str]) -> list[Tag]:
    """Return the Tag rows for ``names`` in the ledger, creating any that are
    new. Deduped by casefold, order preserved by first appearance."""
    result: list[Tag] = []
    seen: set[str] = set()
    for name in names:
        fold = name.strip().casefold()
        if not fold or fold in seen:
            continue
        seen.add(fold)
        ledger_id = ledger.id
        tag = await Tag.where(
            lambda t: (t.ledger_id == ledger_id) & (t.name_fold == fold)
        ).first()
        if tag is None:
            tag = await Tag.create(ledger=ledger, name=name.strip(), name_fold=fold)
        result.append(tag)
    return result
```

- [ ] **Step 4: Write the router**

```python
# src/pinch_backend/api/tags.py
"""/api/v1/tags — free-form labels (PRD M5 #19).

Standard domain conventions: current_ledger (I-2), Page[T], allowlist
responses, tenancy 404s, scope guard by construction.
"""

import uuid
from datetime import datetime

from litestar import Router, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath
from litestar.status_codes import HTTP_409_CONFLICT
from pydantic import BaseModel, Field

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.models import Ledger, Tag, TransactionTag
from pinch_backend.observability import get_logger
from pinch_backend.tags import resolve_tags

log = get_logger(__name__)


class TagCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


def _out(t: Tag) -> TagOut:
    return TagOut(id=t.id, name=t.name, created_at=t.created_at)


@post("/")
async def create_tag(data: TagCreateIn, current_ledger: NamedDependency[Ledger]) -> TagOut:
    fold = data.name.strip().casefold()
    ledger_id = current_ledger.id
    existing = await Tag.where(
        lambda t: (t.ledger_id == ledger_id) & (t.name_fold == fold)
    ).first()
    if existing is not None:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="A tag with that name exists")
    (tag,) = await resolve_tags(current_ledger, [data.name])
    log.info("tag.created", tag_id=str(tag.id), ledger_id=str(current_ledger.id))
    return _out(tag)


@get("/")
async def list_tags(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[TagOut]:
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate(
        Tag.where(lambda t: t.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(items=[_out(t) for t in rows], next_cursor=next_cursor)


@delete("/{tag_id:uuid}")
async def delete_tag(
    tag_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    ledger_id = current_ledger.id
    tag = await Tag.where(lambda t: (t.id == tag_id) & (t.ledger_id == ledger_id)).first()
    if tag is None:
        raise NotFoundException(detail="No such tag")
    await TransactionTag.where(lambda tt: tt.tag_id == tag_id).delete()
    await tag.delete()
    log.info("tag.deleted", tag_id=str(tag_id), ledger_id=str(current_ledger.id))


tags_router = Router(path="/api/v1/tags", route_handlers=[create_tag, list_tags, delete_tag])
```

- [ ] **Step 5: Register the router**

In `src/pinch_backend/api/app.py`:

```python
from pinch_backend.api.tags import tags_router
```

Add `tags_router` to `route_handlers`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_tags_api.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/pinch_backend/api/tags.py src/pinch_backend/tags.py src/pinch_backend/api/app.py tests/test_tags_api.py
git commit -m "feat(api): tags CRUD with implicit creation and casefold uniqueness (M5 CP1, #19)"
```

---

## Task 9: Transaction list + get (filters, composite cursor, inlined category/tags)

**Files:**
- Create: `src/pinch_backend/api/transactions.py`
- Modify: `src/pinch_backend/api/app.py` (register `transactions_router`)
- Test: `tests/test_transactions_api.py` (create)

**Interfaces:**
- Consumes: `paginate_by_date`, `taxonomy.collect_descendant_ids`, `Transaction`, `Category`, `Tag`, `TransactionTag`.
- Produces: router `/api/v1/transactions` with `GET /` (filters below) and `GET /{id}`. `TransactionOut = {id, account_id, date, amount_minor, currency, description_raw, description_normalized, display_name, notes, reviewed_at, category: {id,name}|null, tags: [{id,name}], created_at}`. **No `proposal` field — CP3 adds it.**
- Filters: `account_id: list[uuid]`, `date_from: date`, `date_to: date`, `reviewed: bool`, `category_id: list[uuid]` (subtree-inclusive), `uncategorized: bool`, `tag: list[str]` (AND).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_transactions_api.py
"""/api/v1/transactions list + get (M5 CP1, #19).

Data is created through the real M4 import seam, so these tests also prove
imported transactions carry the normalized payee and are readable back.
"""

TX = "/api/v1/transactions"
IMPORTS = "/api/v1/imports"
ACCOUNTS = "/api/v1/accounts"
PASSWORD = "correct horse battery staple"


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
        ACCOUNTS,
        json={"kind": "depository", "label": "Checking", "currency": "USD"},
        headers=await _csrf(client),
    )
    return r.json()["id"]


async def _import(client, account_id: str, rows: list[tuple[str, str, str]]) -> None:
    body = "date,amount,description\n" + "\n".join(f"{d},{a},{desc}" for d, a, desc in rows) + "\n"
    up = await client.post(
        IMPORTS,
        files={"file": ("bank.csv", body, "text/csv")},
        data={"account_id": account_id},
        headers=await _csrf(client),
    )
    assert up.status_code == 201, up.text
    iid = up.json()["id"]
    await client.post(f"{IMPORTS}/{iid}/mapping", json=up.json()["suggested_mapping"],
                      headers=await _csrf(client))
    commit = await client.post(f"{IMPORTS}/{iid}/commit", json={}, headers=await _csrf(client))
    assert commit.status_code == 200, commit.text


async def test_list_is_newest_first_with_inlined_fields(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [
        ("2026-01-01", "-5.00", "OLDEST"),
        ("2026-03-01", "-7.00", "NEWEST"),
        ("2026-02-01", "-6.00", "MIDDLE"),
    ])
    r = await client.get(TX)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["description_raw"] for i in items] == ["NEWEST", "MIDDLE", "OLDEST"]
    assert items[0]["category"] is None
    assert items[0]["tags"] == []
    assert items[0]["reviewed_at"] is None


async def test_uncategorized_filter_keeps_null_category_rows(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "THING")])
    r = await client.get(f"{TX}?uncategorized=true")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


async def test_composite_cursor_pages_across_a_day_boundary(client) -> None:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [
        ("2026-01-02", "-1.00", "A"),
        ("2026-01-02", "-2.00", "B"),
        ("2026-01-01", "-3.00", "C"),
    ])
    page1 = await client.get(f"{TX}?limit=2")
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    page2 = await client.get(f"{TX}?limit=2&cursor={body1['next_cursor']}")
    body2 = page2.json()
    assert len(body2["items"]) == 1
    seen = [i["description_raw"] for i in body1["items"] + body2["items"]]
    assert len(set(seen)) == 3  # no dupes, no gaps across the boundary


async def test_other_ledger_transaction_is_a_404(client) -> None:
    await _signup(client, "a@example.com")
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "MINE")])
    mine_id = (await client.get(TX)).json()["items"][0]["id"]
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, "b@example.com")
    r = await client.get(f"{TX}/{mine_id}")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transactions_api.py -v`
Expected: FAIL — routes 404.

- [ ] **Step 3: Write the router**

```python
# src/pinch_backend/api/transactions.py
"""/api/v1/transactions — the transaction list, get, and user-data PATCH
(PRD M5 #19).

The inbox and every classification screen read from this list: it inlines
the assigned category and tags (batch-fetched per page — no N+1, and never
via INNER-join traversal that would drop uncategorized rows), and orders
newest-first behind a composite (date, id) keyset cursor. current_ledger
(I-2), Page[T], allowlist responses, tenancy 404s, scope guard by
construction throughout.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from litestar import Router, get
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.params import FromPath, QueryParameter
from pydantic import BaseModel

from pinch_backend import taxonomy
from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate_by_date,
)
from pinch_backend.models import Category, Ledger, Tag, Transaction, TransactionTag
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class CategoryRef(BaseModel):
    id: uuid.UUID
    name: str


class TagRef(BaseModel):
    id: uuid.UUID
    name: str


class TransactionOut(BaseModel):
    """What a client may see about a transaction — an allowlist (M5 CP1).
    A ``proposal`` field is added additively in CP3."""

    id: uuid.UUID
    account_id: uuid.UUID
    date: date
    amount_minor: int
    currency: str
    description_raw: str
    description_normalized: str
    display_name: str | None
    notes: str | None
    reviewed_at: datetime | None
    category: CategoryRef | None
    tags: list[TagRef]
    created_at: datetime


async def _get(ledger: Ledger, txn_id: uuid.UUID) -> Transaction:
    txn = await Transaction.where(
        lambda t: (t.id == txn_id) & (t.ledger_id == ledger.id)
    ).first()
    if txn is None:
        raise NotFoundException(detail="No such transaction")
    return txn


async def _out_page(txns: list[Transaction]) -> list[TransactionOut]:
    """Batch-hydrate categories and tags for a page in two queries each,
    never per-row."""
    cat_ids = sorted({t.category_id for t in txns if t.category_id is not None})
    cats = (
        {c.id: c for c in await Category.where(lambda c: c.id.in_(cat_ids)).all()}
        if cat_ids
        else {}
    )
    txn_ids = [t.id for t in txns]
    links = (
        await TransactionTag.where(lambda tt: tt.transaction_id.in_(txn_ids)).all()
        if txn_ids
        else []
    )
    tag_ids = sorted({link.tag_id for link in links})
    tags = (
        {t.id: t for t in await Tag.where(lambda t: t.id.in_(tag_ids)).all()}
        if tag_ids
        else {}
    )
    by_txn: dict[uuid.UUID, list[TagRef]] = {}
    for link in links:
        tag = tags[link.tag_id]
        by_txn.setdefault(link.transaction_id, []).append(TagRef(id=tag.id, name=tag.name))
    result = []
    for t in txns:
        cat = cats.get(t.category_id) if t.category_id else None
        result.append(
            TransactionOut(
                id=t.id,
                account_id=t.account_id,
                date=t.date,
                amount_minor=t.amount_minor,
                currency=t.currency,
                description_raw=t.description_raw,
                description_normalized=t.description_normalized,
                display_name=t.display_name,
                notes=t.notes,
                reviewed_at=t.reviewed_at,
                category=CategoryRef(id=cat.id, name=cat.name) if cat else None,
                tags=by_txn.get(t.id, []),
                created_at=t.created_at,
            )
        )
    return result


@get("/")
async def list_transactions(
    current_ledger: NamedDependency[Ledger],
    account_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    date_from: Annotated[date | None, QueryParameter()] = None,
    date_to: Annotated[date | None, QueryParameter()] = None,
    reviewed: Annotated[bool | None, QueryParameter()] = None,
    category_id: Annotated[list[uuid.UUID] | None, QueryParameter()] = None,
    uncategorized: Annotated[bool | None, QueryParameter()] = None,
    tag: Annotated[list[str] | None, QueryParameter()] = None,
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[TransactionOut]:
    ledger_id = current_ledger.id
    query = Transaction.where(lambda t: t.ledger_id == ledger_id)

    if account_id:
        accounts = list(account_id)
        query = query.where(lambda t: t.account_id.in_(accounts))
    if date_from is not None:
        start = date_from
        query = query.where(lambda t: t.date >= start)
    if date_to is not None:
        end = date_to
        query = query.where(lambda t: t.date <= end)
    if reviewed is True:
        query = query.where(lambda t: t.reviewed_at != None)  # noqa: E711
    elif reviewed is False:
        query = query.where(lambda t: t.reviewed_at == None)  # noqa: E711
    if uncategorized:
        query = query.where(lambda t: t.category_id == None)  # noqa: E711
    if category_id:
        subtree = await taxonomy.collect_descendant_ids(list(category_id), ledger_id)
        ids = sorted(subtree)
        query = query.where(lambda t: t.category_id.in_(ids))
    if tag:
        wanted = list(tag)
        wanted_folds = sorted({name.strip().casefold() for name in wanted})
        matched_tags = await Tag.where(
            lambda t: (t.ledger_id == ledger_id) & (t.name_fold.in_(wanted_folds))
        ).all()
        if len(matched_tags) < len(wanted_folds):
            return Page(items=[], next_cursor=None)  # an unknown tag matches nothing
        keep: set[uuid.UUID] | None = None
        for tg in matched_tags:
            tid = tg.id
            links = await TransactionTag.where(lambda tt: tt.tag_id == tid).all()
            ids_for_tag = {link.transaction_id for link in links}
            keep = ids_for_tag if keep is None else (keep & ids_for_tag)
        keep_ids = sorted(keep or set())
        if not keep_ids:
            return Page(items=[], next_cursor=None)
        query = query.where(lambda t: t.id.in_(keep_ids))

    rows, next_cursor = await paginate_by_date(query, cursor=cursor, limit=limit)
    return Page(items=await _out_page(rows), next_cursor=next_cursor)


@get("/{txn_id:uuid}")
async def get_transaction(
    txn_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> TransactionOut:
    txn = await _get(current_ledger, txn_id)
    (out,) = await _out_page([txn])
    return out


transactions_router = Router(
    path="/api/v1/transactions",
    route_handlers=[list_transactions, get_transaction],
)
```

- [ ] **Step 4: Register the router**

In `src/pinch_backend/api/app.py`:

```python
from pinch_backend.api.transactions import transactions_router
```

Add `transactions_router` to `route_handlers`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_transactions_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pinch_backend/api/transactions.py src/pinch_backend/api/app.py tests/test_transactions_api.py
git commit -m "feat(api): transaction list + get with filters and composite cursor (M5 CP1, #19)"
```

---

## Task 10: Transaction user-data PATCH

> **Correction (post-implementation, shipped in commits 10b2c8a + 03eeeed):**
> (1) `txn.category = category` must be `txn.category_id = category.id` (or
> `None` to clear) — same ferro relation-ClassVar issue as Task 7. (2) The
> handler's writes (`txn.save()`, `resolve_tags`, and the tag detach/attach
> loop) must run inside one `async with transaction():` (import `transaction`
> from ferro), with the ledger-scoped category 404 lookup done before the
> block — otherwise a mid-reconcile failure leaves partial writes. A tag
> detach/idempotency test was added. Shipped code is authoritative.

**Files:**
- Modify: `src/pinch_backend/api/transactions.py` (add `patch` handler + input model; register it)
- Test: `tests/test_transactions_api.py` (add PATCH tests)

**Interfaces:**
- Consumes: `resolve_tags`, `_get`, `_out_page`, `Category`, `TransactionTag`.
- Produces: `PATCH /api/v1/transactions/{id}` — allowlist `category_id` (null clears), `tags` (list of names, full reconcile), `display_name`, `notes`, `reviewed` (bool → sets/clears `reviewed_at`). Fields absent from the body are left unchanged (uses `model_fields_set`).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_transactions_api.py


async def _one_txn(client) -> str:
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, [("2026-01-01", "-5.00", "COFFEE SHOP")])
    return (await client.get(TX)).json()["items"][0]["id"]


async def test_patch_assigns_category_tags_and_reviews(client) -> None:
    txn_id = await _one_txn(client)
    cat = (await client.post("/api/v1/categories", json={"name": "Coffee"},
                             headers=await _csrf(client))).json()
    r = await client.patch(
        f"{TX}/{txn_id}",
        json={
            "category_id": cat["id"],
            "tags": ["morning", "Morning"],  # casefold-deduped to one
            "display_name": "Blue Bottle",
            "notes": "oat latte",
            "reviewed": True,
        },
        headers=await _csrf(client),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["category"]["id"] == cat["id"]
    assert [t["name"] for t in body["tags"]] == ["morning"]
    assert body["display_name"] == "Blue Bottle"
    assert body["notes"] == "oat latte"
    assert body["reviewed_at"] is not None


async def test_patch_can_clear_category_and_unreview(client) -> None:
    txn_id = await _one_txn(client)
    cat = (await client.post("/api/v1/categories", json={"name": "Coffee"},
                             headers=await _csrf(client))).json()
    await client.patch(f"{TX}/{txn_id}", json={"category_id": cat["id"], "reviewed": True},
                       headers=await _csrf(client))
    r = await client.patch(
        f"{TX}/{txn_id}", json={"category_id": None, "reviewed": False},
        headers=await _csrf(client),
    )
    body = r.json()
    assert body["category"] is None
    assert body["reviewed_at"] is None


async def test_patch_leaves_unmentioned_fields_untouched(client) -> None:
    txn_id = await _one_txn(client)
    await client.patch(f"{TX}/{txn_id}", json={"notes": "keep me"}, headers=await _csrf(client))
    r = await client.patch(f"{TX}/{txn_id}", json={"display_name": "Renamed"},
                           headers=await _csrf(client))
    assert r.json()["notes"] == "keep me"  # not wiped by the second patch


async def test_read_scoped_pat_cannot_patch(client) -> None:
    txn_id = await _one_txn(client)
    pat = await client.post(
        "/api/v1/auth/pats", json={"name": "ro", "scopes": ["read"]},
        headers=await _csrf(client),
    )
    token = pat.json()["token"]
    r = await client.patch(
        f"{TX}/{txn_id}", json={"notes": "nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# --- Filter round-trip tests (added per Task 9 review): the category/tag/
# reviewed filters can only be exercised once PATCH exists to assign those
# values, so their integration coverage lands here, at the real seam. ---


async def _setup_txns(client, rows):
    """Sign up, import `rows`, and return a {description: transaction_id} map."""
    await _signup(client)
    acct = await _account(client)
    await _import(client, acct, rows)
    items = (await client.get(f"{TX}?limit=100")).json()["items"]
    return {i["description_raw"]: i["id"] for i in items}


async def test_category_id_filter_is_subtree_inclusive(client) -> None:
    ids = await _setup_txns(client, [("2026-01-01", "-5.00", "DINNER")])
    food = (await client.post("/api/v1/categories", json={"name": "Food2"},
                              headers=await _csrf(client))).json()
    rest = (await client.post(
        "/api/v1/categories", json={"name": "Restaurants2", "parent_id": food["id"]},
        headers=await _csrf(client),
    )).json()
    await client.patch(f"{TX}/{ids['DINNER']}", json={"category_id": rest["id"]},
                       headers=await _csrf(client))
    # Filtering by the PARENT returns the child-categorized transaction.
    r = await client.get(f"{TX}?category_id={food['id']}")
    assert [i["description_raw"] for i in r.json()["items"]] == ["DINNER"]


async def test_tag_filter_is_and_composition(client) -> None:
    ids = await _setup_txns(client, [
        ("2026-01-02", "-1.00", "BOTH"),
        ("2026-01-01", "-2.00", "ONE"),
    ])
    await client.patch(f"{TX}/{ids['BOTH']}", json={"tags": ["x", "y"]},
                       headers=await _csrf(client))
    await client.patch(f"{TX}/{ids['ONE']}", json={"tags": ["x"]},
                       headers=await _csrf(client))
    r = await client.get(f"{TX}?tag=x&tag=y")  # AND: only the row with both
    assert [i["description_raw"] for i in r.json()["items"]] == ["BOTH"]
    r2 = await client.get(f"{TX}?tag=x")  # single tag: both rows
    assert {i["description_raw"] for i in r2.json()["items"]} == {"BOTH", "ONE"}
    r3 = await client.get(f"{TX}?tag=x&tag=nope")  # unknown tag: empty, not ignored
    assert r3.json()["items"] == []


async def test_reviewed_filter_splits_inbox_from_done(client) -> None:
    ids = await _setup_txns(client, [
        ("2026-01-02", "-1.00", "DONE"),
        ("2026-01-01", "-2.00", "TODO"),
    ])
    await client.patch(f"{TX}/{ids['DONE']}", json={"reviewed": True},
                       headers=await _csrf(client))
    r = await client.get(f"{TX}?reviewed=true")
    assert [i["description_raw"] for i in r.json()["items"]] == ["DONE"]
    r2 = await client.get(f"{TX}?reviewed=false")
    assert [i["description_raw"] for i in r2.json()["items"]] == ["TODO"]


async def test_uncategorized_filter_excludes_categorized_rows(client) -> None:
    ids = await _setup_txns(client, [
        ("2026-01-02", "-1.00", "HASCAT"),
        ("2026-01-01", "-2.00", "NOCAT"),
    ])
    cat = (await client.post("/api/v1/categories", json={"name": "Misc2"},
                             headers=await _csrf(client))).json()
    await client.patch(f"{TX}/{ids['HASCAT']}", json={"category_id": cat["id"]},
                       headers=await _csrf(client))
    r = await client.get(f"{TX}?uncategorized=true")
    assert [i["description_raw"] for i in r.json()["items"]] == ["NOCAT"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transactions_api.py -k patch -v`
Expected: FAIL — 405/404 (no PATCH handler).

- [ ] **Step 3: Add the PATCH handler**

In `src/pinch_backend/api/transactions.py`, add imports:

```python
from litestar import Router, get, patch
from pydantic import BaseModel, ConfigDict
from pinch_backend.tags import resolve_tags
```

Add the input model near `TransactionOut`:

```python
class TransactionPatchIn(BaseModel):
    """User-data allowlist (M5). Only the fields present in the request body
    are applied — source data (date, amount, description, fingerprint) is not
    addressable here."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    category_id: uuid.UUID | None = None
    """Present-and-null clears the category (→ uncategorized)."""
    tags: list[str] | None = None
    """The complete tag set for the transaction; reconciled (implicit-create
    new names, detach removed ones). Present-and-empty clears all tags."""
    display_name: str | None = None
    notes: str | None = None
    reviewed: bool | None = None
    """True sets reviewed_at to now; False clears it (back to the inbox)."""
```

Add the handler:

```python
@patch("/{txn_id:uuid}")
async def patch_transaction(
    txn_id: FromPath[uuid.UUID],
    data: TransactionPatchIn,
    current_ledger: NamedDependency[Ledger],
) -> TransactionOut:
    from pinch_backend.models import utcnow

    txn = await _get(current_ledger, txn_id)
    fields = data.model_fields_set

    if "category_id" in fields:
        if data.category_id is not None:
            category = await Category.where(
                lambda c: (c.id == data.category_id) & (c.ledger_id == current_ledger.id)
            ).first()
            if category is None:
                raise NotFoundException(detail="No such category")
            txn.category = category
        else:
            txn.category = None
    if "display_name" in fields:
        txn.display_name = data.display_name
    if "notes" in fields:
        txn.notes = data.notes
    if "reviewed" in fields:
        txn.reviewed_at = utcnow() if data.reviewed else None
    await txn.save()

    if "tags" in fields:
        wanted = await resolve_tags(current_ledger, data.tags or [])
        wanted_ids = {t.id for t in wanted}
        tid = txn.id
        existing = await TransactionTag.where(lambda tt: tt.transaction_id == tid).all()
        existing_ids = {tt.tag_id for tt in existing}
        for tt in existing:
            if tt.tag_id not in wanted_ids:
                await tt.delete()
        for tg in wanted:
            if tg.id not in existing_ids:
                await TransactionTag.create(ledger=current_ledger, transaction=txn, tag=tg)

    log.info("transaction.updated", transaction_id=str(txn.id), ledger_id=str(current_ledger.id))
    (out,) = await _out_page([txn])
    return out
```

Register it in the router:

```python
transactions_router = Router(
    path="/api/v1/transactions",
    route_handlers=[list_transactions, get_transaction, patch_transaction],
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transactions_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pinch_backend/api/transactions.py tests/test_transactions_api.py
git commit -m "feat(api): transaction user-data PATCH — category, tags, display name, notes, review (M5 CP1, #19)"
```

---

## Task 11: Full-suite gate on both backends + open the draft PR

**Files:** none (verification + PR).

- [ ] **Step 1: Run the entire suite on sqlite**

Run: `uv run pytest -q`
Expected: PASS (all M4 tests + the new CP1 tests).

- [ ] **Step 2: Run the entire suite on Postgres**

Run: `PINCH_TEST_DATABASE_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest -q`
Expected: PASS (per the `local-pg` docker container; both-backend parity is the M4 discipline).

- [ ] **Step 3: Lint/type gate**

Run: `uv run prek run --all-files` (or the project's configured hook runner)
Expected: ruff, ruff-format, ty all PASS.

- [ ] **Step 4: Push the branch and open a draft PR**

```bash
git push -u origin m5-classification
gh pr create --draft --title "M5: classification pipeline, review inbox, rules, correction log (#18)" \
  --body "$(cat <<'BODY'
Delivers PRD M5 (#18) across its four sub-issues on one branch.

- [x] CP1 (#19): taxonomy, tags, transaction user-data surface, transaction list
- [ ] CP2 (#20): rules, evaluator, preview
- [ ] CP3 (#21): Procrastinate, pipeline, proposals, correction log, auto-file
- [ ] CP4 (#22): review, promotion, manual entry

CP1 seeds an editable two-level taxonomy on provisioning, adds first-class
tags, lands the transaction user-data columns (category, display name,
notes, reviewed state) plus the normalized payee CP3 will match on, and
ships the date-ordered transaction list/get/patch behind a composite
(date, id) keyset cursor.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 5: Update memory**

Append CP1-complete status to the M5 memory note (`m5-prd-grilled.md`): CP1 shipped on `m5-classification`, draft PR open, CP2 is next.

---

## Self-Review

**Spec coverage:**
- Migration flags → Task 1. ✓
- Models (Category, Tag, TransactionTag, Transaction columns) → Task 2. ✓
- `description_normalized` populated at write → Task 3 (imports). ✓
- Tree helpers (depth, cycle, descendants) → Task 4. ✓
- Seeding + `provision_user` wiring → Task 5. ✓
- Composite cursor → Task 6. ✓
- Categories API (CRUD, depth/cycle, delete-with-disposition, children-block) → Task 7. ✓
- Tags API (implicit create, casefold uniqueness, detach-on-delete) → Task 8. ✓
- Transaction list/get (filter matrix, uncategorized-survives, composite cursor, inlined category/tags, no N+1) → Task 9. ✓
- Transaction PATCH (allowlist, tag reconcile, unmentioned-untouched, read-PAT 403) → Task 10. ✓
- Both-backend gate + PR → Task 11. ✓
- Tenancy 404s → Tasks 7, 9 tests. ✓

**Deviations from the design doc (intentional, noted):**
- Category sibling-name uniqueness is enforced app-side (`_assert_sibling_name_free`) rather than solely by a DB composite unique, because a DB unique on the nullable `parent_id` cannot cover top-level siblings (NULLs compare distinct). The design's `(ledger_id, parent_id, name)` unique is dropped in favor of the app check to avoid a partial guarantee.
- The category re-parent PATCH uses an explicit `reparent: bool` flag to distinguish "move to top level" (parent_id null, reparent true) from "leave parent alone" (reparent false), since a nullable field can't express both. This refines the design's "PATCH (rename + re-parent)" without changing its contract.

**Placeholder scan:** none — every code step carries complete code.

**Type consistency:** `_out_page` used in Tasks 9 and 10 with the same signature; `resolve_tags` defined in Task 8, consumed in Tasks 8 and 10; `paginate_by_date` defined in Task 6, consumed in Task 9; `collect_descendant_ids` defined in Task 4, consumed in Task 9. Names verified consistent across tasks.

**Resolved during planning:** `ClientException(detail=..., status_code=409)` accepts `status_code` (verified), so Task 7's children-block 409 needs no `HTTPException` fallback.
