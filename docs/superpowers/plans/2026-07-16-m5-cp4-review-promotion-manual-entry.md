# M5 CP4 — Review, Promotion, Manual Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The human half of the flywheel — review endpoints wrapping CP3's `consume_proposal`, consented rule promotion reading the correction log, manual transaction entry, and the un-review round-trip.

**Architecture:** Two new modules (`api/reviews.py` for both review endpoints on their own Router at `/api/v1/transactions`; `classification/promotion.py` for the log-reading promotion engine) plus surgical changes to `api/transactions.py` (PATCH consume/defer integration, manual-entry POST), `api/rules.py` (`_out` → public `rule_out`, delete docstring), and `tags.py` (shared `dedupe_tag_names`). Everything wraps CP3 seams; no schema changes.

**Tech Stack:** Litestar, ferro-orm 0.16.2 (Postgres only), Procrastinate (in-memory connector in tests), pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-07-16-m5-cp4-review-promotion-manual-entry-design.md` — read it first; it is the contract.

## Global Constraints

- Commits: conventional, ending `(M5 CP4, #22)`, trailer EXACTLY `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (a subagent once signed its own model name and failed review on it).
- Tests: Postgres only — plain `uv run pytest` hits the local-pg docker container; baseline 331 green. No live network. Procrastinate via the autouse in-memory connector; use the `run_jobs` fixture; job effects asserted at the HTTP seam.
- ferro: relations are ClassVars — assign shadow FKs (`txn.category_id = ...`), never the relation attr; instance shadow-FK *reads* need scoped `# ty: ignore[unresolved-attribute]`; `.create()` kwargs are untyped (no ignore needed there).
- Multi-step writes inside `async with transaction():`. Nested `transaction()` is atomic with the outer (scratch-verified 2026-07-16 against local-pg: outer rollback undoes inner writes).
- ruff B023: bind loop variables as lambda default args in `where()` predicates (`lambda t, tid=txn_id: ...`).
- `== None` / `!= None` predicates carry `# noqa: E711`.
- Annotation-only imports go in `TYPE_CHECKING` blocks with quoted annotations — never `# noqa: TC003`.
- Defer jobs AFTER the ferro transaction commits (`await classify_ledger.configure(lock=f"ledger:{ledger_id}").defer_async(...)` — the imports.py precedent).
- API conventions: `current_ledger: NamedDependency[Ledger]` (I-2), allowlist Out models, tenancy misses answer 404 (never 403), 409 via `HTTPException(status_code=HTTP_409_CONFLICT, detail=...)` (the imports.py `_conflict` pattern), structured events via `log.info("noun.verb", ...)`.
- docs/superpowers is gitignored — plan/spec commits use `git add -f`.

---

### Task 1: Promotion engine

**Files:**
- Create: `src/pinch_backend/classification/promotion.py`
- Create: `tests/test_promotion.py`

**Interfaces:**
- Consumes: `CorrectionLogEntry`, `CorrectionActor`, `CorrectionKind`, `Rule`, `RuleStatus`, `Ledger` (models.py); `ConditionSpec`, `PayeeCondition` (rules/spec.py); `normalize_description` (imports/fingerprint.py).
- Produces: `maybe_propose_rule(ledger: Ledger, payee: str, category_id: "uuid.UUID | None") -> Rule | None` and `MIN_PROMOTION_DECISIONS = 3` — Tasks 2–5 call this after every user-actor consume.

- [ ] **Step 1: Write the failing tests**

`tests/test_promotion.py` — model seam (the `db` fixture), one helper:

```python
"""Promotion (M5 CP4, #22): >=3 consistent user-actor non-voided log
decisions filing payee X as category Y, no rule in ANY state covering X ->
mint `payee equals` in status=proposed. Latest decision per transaction
wins, derived here, never stored. Auto decisions are never evidence."""

import uuid

import pytest

from pinch_backend.classification.promotion import maybe_propose_rule
from pinch_backend.models import (
    Category,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Rule,
    RuleStatus,
)

pytestmark = pytest.mark.anyio

PAYEE = "starbucks 123"


async def _ledger() -> tuple[Ledger, Category]:
    ledger = await Ledger.create(name="promo")
    coffee = await Category.create(ledger=ledger, name="Coffee")
    return ledger, coffee


async def _decision(
    ledger: Ledger,
    category: Category | None,
    *,
    payee: str = PAYEE,
    txn_id: uuid.UUID | None = None,
    actor: CorrectionActor = CorrectionActor.USER,
) -> CorrectionLogEntry:
    return await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=txn_id or uuid.uuid7(),
        kind=CorrectionKind.DECISION,
        actor=actor,
        input_payee=payee,
        decision_category_id=category.id if category else None,
        decision_category_name=category.name if category else None,
    )


async def test_three_consistent_user_decisions_mint_a_proposed_rule(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee)
    rule = await maybe_propose_rule(ledger, PAYEE, coffee.id)
    assert rule is not None
    assert rule.status == RuleStatus.PROPOSED
    assert rule.condition["payee"] == {"op": "equals", "value": PAYEE}
    assert rule.action_category_id == coffee.id  # ty: ignore[unresolved-attribute]
    assert rule.action_add_tags == []
    assert rule.action_rename_to is None


async def test_two_decisions_do_not_mint(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(2):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_one_deviation_kills(db) -> None:
    ledger, coffee = await _ledger()
    dining = await Category.create(ledger=ledger, name="Dining")
    for _ in range(3):
        await _decision(ledger, coffee)
    await _decision(ledger, dining)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_an_uncategorized_latest_decision_kills(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee)
    await _decision(ledger, None)  # the user decided "uncategorized"
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_auto_decisions_are_never_evidence(db) -> None:
    """The pollution guard, by name (PRD M5)."""
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee, actor=CorrectionActor.AUTO)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None
    await _decision(ledger, coffee)
    await _decision(ledger, coffee)
    # 2 user + 3 auto: still short of three VOTES.
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_a_later_auto_entry_supersedes_that_transactions_user_vote(db) -> None:
    ledger, coffee = await _ledger()
    txn_id = uuid.uuid7()
    await _decision(ledger, coffee, txn_id=txn_id)  # user decided...
    await _decision(ledger, coffee, txn_id=txn_id, actor=CorrectionActor.AUTO)  # ...then auto re-filed
    await _decision(ledger, coffee)
    await _decision(ledger, coffee)
    # The superseded transaction casts no vote: 2 votes, no mint.
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_voided_decisions_are_excluded(db) -> None:
    ledger, coffee = await _ledger()
    entries = [await _decision(ledger, coffee) for _ in range(3)]
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=entries[0].transaction_id,
        kind=CorrectionKind.VOID,
        actor=CorrectionActor.USER,
        voids=entries[0].id,
        void_reason="import undone",
    )
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_latest_decision_per_transaction_wins(db) -> None:
    """A changed mind repairs consistency: the deviation's transaction was
    re-decided to Y, so all three latest decisions agree."""
    ledger, coffee = await _ledger()
    dining = await Category.create(ledger=ledger, name="Dining")
    txn_id = uuid.uuid7()
    await _decision(ledger, dining, txn_id=txn_id)  # the old deviation
    await _decision(ledger, coffee, txn_id=txn_id)  # re-decided
    await _decision(ledger, coffee)
    await _decision(ledger, coffee)
    rule = await maybe_propose_rule(ledger, PAYEE, coffee.id)
    assert rule is not None


@pytest.mark.parametrize("status", list(RuleStatus))
async def test_a_covering_equals_rule_in_any_state_blocks(db, status: RuleStatus) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=status,
        condition={"version": 1, "payee": {"op": "equals", "value": PAYEE}},
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_a_covering_contains_rule_blocks(db) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=RuleStatus.ACTIVE,
        condition={"version": 1, "payee": {"op": "contains", "value": "STARBUCKS"}},
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    # The stored value normalizes to "starbucks", a substring of the payee.
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None


async def test_a_rule_without_a_payee_clause_does_not_block(db) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=RuleStatus.ACTIVE,
        condition={
            "version": 1,
            "amount": {"op": "equals", "value": 500, "direction": "out", "currency": "USD"},
        },
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is not None


async def test_a_non_matching_payee_rule_does_not_block(db) -> None:
    ledger, coffee = await _ledger()
    await Rule.create(
        ledger=ledger,
        status=RuleStatus.ACTIVE,
        condition={"version": 1, "payee": {"op": "equals", "value": "peets"}},
        action_category_id=coffee.id,
    )
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is not None


async def test_null_category_never_checks(db) -> None:
    ledger, coffee = await _ledger()
    for _ in range(3):
        await _decision(ledger, coffee)
    assert await maybe_propose_rule(ledger, PAYEE, None) is None


async def test_unexpressible_payees_never_mint(db) -> None:
    """PayeeCondition.value is bounded 1-200; a payee outside those bounds
    simply never promotes (it cannot be expressed as a rule)."""
    ledger, coffee = await _ledger()
    long_payee = "x" * 201
    for _ in range(3):
        await _decision(ledger, coffee, payee=long_payee)
    assert await maybe_propose_rule(ledger, long_payee, coffee.id) is None


async def test_evidence_is_ledger_scoped(db) -> None:
    ledger, coffee = await _ledger()
    other, other_coffee = await _ledger()
    for _ in range(2):
        await _decision(ledger, coffee)
    await _decision(other, other_coffee)
    assert await maybe_propose_rule(ledger, PAYEE, coffee.id) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_promotion.py -x -q`
Expected: FAIL at import — `No module named 'pinch_backend.classification.promotion'`.

- [ ] **Step 3: Write the implementation**

`src/pinch_backend/classification/promotion.py`:

```python
"""Consented rule promotion (PRD M5 D14, M5 CP4 #22): inline at review
time, scoped to the just-reviewed payee. Trigger: >=3 user-actor, non-voided
log decisions filing payee X as category Y, all-time consistency (one
deviation kills — mixed payees are AI territory), and no rule in ANY state
covering X. Mints `payee equals` (never `contains` — auto-minted substring
rules are a footgun) in status=proposed; accepting is a status flip
(PATCH /rules/{id}).

Promotion reads the LOG; history reads transactions — the log answers "what
did the user decide", transactions answer "how are things filed now".
"Latest decision per transaction wins" is derived here, never stored. Auto
entries are never evidence, but a later auto entry supersedes that
transaction's user vote (the user's decision is no longer the standing one).

Called AFTER the consume transaction commits: a minting failure never rolls
back a review. Two same-payee reviews racing could double-mint — a
documented residual (single-tenant, microsecond window), same class as the
sweep's TOCTOU notes.
"""

from typing import TYPE_CHECKING

from pinch_backend.imports.fingerprint import normalize_description
from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Rule,
    RuleStatus,
)
from pinch_backend.observability import get_logger
from pinch_backend.rules.spec import ConditionSpec, PayeeCondition

if TYPE_CHECKING:
    import uuid

log = get_logger(__name__)

MIN_PROMOTION_DECISIONS = 3
"""Consistent user filings before a rule is proposed (PRD M5 D14)."""

_MAX_PAYEE_CONDITION_LENGTH = 200
"""PayeeCondition.value's upper bound; longer payees are unexpressible as
rule conditions and simply never promote."""


def _covers(rule: Rule, payee: str) -> bool:
    """Does this rule's payee clause match ``payee``? Evaluator semantics
    (rules.evaluator.matches), minus the stages promotion doesn't test:
    a rule without a payee clause never covers a payee."""
    spec = ConditionSpec(**rule.condition)
    if spec.payee is None:
        return False
    needle = normalize_description(spec.payee.value)
    if spec.payee.op == "equals":
        return payee == needle
    return needle in payee


async def maybe_propose_rule(
    ledger: Ledger, payee: str, category_id: "uuid.UUID | None"
) -> Rule | None:
    """The inline promotion check. ``category_id`` is the just-decided
    category (Y); the just-appended log entry is already evidence because
    this runs after the consume transaction commits."""
    if category_id is None:
        return None
    if not payee or len(payee) > _MAX_PAYEE_CONDITION_LENGTH:
        return None

    ledger_id = ledger.id
    entries = (
        await CorrectionLogEntry.where(
            lambda e, lid=ledger_id, p=payee: (
                (e.ledger_id == lid) & (e.kind == CorrectionKind.DECISION) & (e.input_payee == p)
            )
        )
        .order_by(lambda e: e.id)
        .all()
    )
    if len(entries) < MIN_PROMOTION_DECISIONS:
        return None
    entry_ids = [e.id for e in entries]
    voided = {
        v.voids
        for v in await CorrectionLogEntry.where(
            lambda v, ids=entry_ids: v.voids.in_(ids)
        ).all()
    }
    latest: dict[uuid.UUID, CorrectionLogEntry] = {}
    for entry in entries:  # id-ascending (uuid7): the last write wins
        if entry.id in voided:
            continue
        latest[entry.transaction_id] = entry
    votes = [e for e in latest.values() if e.actor == CorrectionActor.USER]
    if len(votes) < MIN_PROMOTION_DECISIONS:
        return None
    if any(vote.decision_category_id != category_id for vote in votes):
        return None  # one deviation kills (uncategorized was a decision too)

    rules = await Rule.where(lambda r, lid=ledger_id: r.ledger_id == lid).all()
    if any(_covers(rule, payee) for rule in rules):
        return None  # ANY state: proposed awaits consent, dismissed is a tombstone

    condition = ConditionSpec(payee=PayeeCondition(op="equals", value=payee))
    rule = await Rule.create(
        ledger=ledger,
        status=RuleStatus.PROPOSED,
        condition=condition.model_dump(exclude_none=True),
        action_category_id=category_id,
    )
    log.info(
        "rule.promoted",
        rule_id=str(rule.id),
        ledger_id=str(ledger_id),
        payee=payee,
        category_id=str(category_id),
        decisions=len(votes),
    )
    return rule
```

Note the `latest` dict type annotation needs `uuid` at runtime? No — it's a
local annotation inside a function body, evaluated lazily under PEP 649;
`import uuid` stays in TYPE_CHECKING. If ty complains, quote it:
`latest: "dict[uuid.UUID, CorrectionLogEntry]" = {}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_promotion.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ty check
git add src/pinch_backend/classification/promotion.py tests/test_promotion.py
git commit -m "feat(classification): consented rule promotion engine (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Single-transaction review endpoint

**Files:**
- Create: `src/pinch_backend/api/reviews.py`
- Modify: `src/pinch_backend/api/rules.py` (rename `_out` → public `rule_out`; update its 4 call sites)
- Modify: `src/pinch_backend/tags.py` (add `dedupe_tag_names`)
- Modify: `src/pinch_backend/api/app.py` (register `reviews_router`)
- Create: `tests/test_reviews_api.py`

**Interfaces:**
- Consumes: `consume_proposal(ledger, txn, *, category_id, tags, display_name, actor)` (classification/consume.py); `maybe_propose_rule(ledger, payee, category_id)` (Task 1); `hydrate_transactions`, `TransactionOut` (api/transactions.py); `RuleOut` (api/rules.py).
- Produces: `reviews_router` (Router at `/api/v1/transactions`); `ReviewIn`, `ReviewOut`, `_pending_proposal(txn_id) -> tuple[Proposal | None, list[str]]` (Task 3 reuses); `rule_out(rule) -> RuleOut` public in rules.py; `dedupe_tag_names(names: list[str]) -> list[str]` in tags.py (Tasks 3–5 reuse).

- [ ] **Step 1: Add `dedupe_tag_names` to `tags.py`**

Append to `src/pinch_backend/tags.py`:

```python
def dedupe_tag_names(names: list[str]) -> list[str]:
    """Trim + casefold-dedupe, first casing wins — the same fold rule as
    resolve_tags. Review payloads normalize BEFORE consume (M5 CP4) so
    decision_tags logs exactly the applied set, never a raw superset."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        fold = name.strip().casefold()
        if fold and fold not in seen:
            seen.add(fold)
            out.append(name.strip())
    return out
```

- [ ] **Step 2: Rename `_out` → `rule_out` in `api/rules.py`**

Rename the function and its 4 internal call sites (create_rule, list_rules, get_rule, update_rule). Docstring gains: `Public: review responses embed the minted rule (M5 CP4).` Run `uv run pytest tests/test_rules_api.py -q` — still green.

- [ ] **Step 3: Write the failing tests**

`tests/test_reviews_api.py` (helpers mirror test_classification_api.py — copy `_csrf`, `_signup`, `_account`, `_commit_csv`, `_category`, `_transactions`, `MAPPING`, `PASSWORD` verbatim from that file):

```python
"""POST /transactions/{id}/review (M5 CP4, #22): the body carries the FINAL
user data; the server diffs against the proposal to record accepted-vs-
corrected; empty body accepts as-is. Wraps CP3's consume."""

TX = "/api/v1/transactions"
LOG = "/api/v1/correction-log"
RULES = "/api/v1/rules"

# ... helpers as noted above ...


async def _review(client, txn_id: str, body: dict | None = None):
    return await client.post(
        f"{TX}/{txn_id}/review", json=body or {}, headers=await _csrf(client)
    )


async def _inbox_txn(client) -> dict:
    items = [t for t in await _transactions(client) if t["reviewed_at"] is None]
    assert items, "expected an unreviewed transaction"
    return items[0]


async def test_empty_body_accepts_the_proposal_as_is(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    # History seed: review the first arrival with a correction...
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    first = await _inbox_txn(client)
    r = await _review(client, first["id"], {"category_id": coffee})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "corrected"  # empty proposal vs a category
    # ...so the second arrival is history-proposed and accepting is a no-diff.
    await _commit_csv(client, account_id, rows=[("2026-07-02", "-6.00", "STARBUCKS 123")])
    await run_jobs()
    second = await _inbox_txn(client)
    assert second["proposal"]["provenance"] == "history"
    r2 = await _review(client, second["id"])
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["result"] == "accepted"
    assert body["transaction"]["reviewed_at"] is not None
    assert body["transaction"]["category"]["id"] == coffee
    assert body["transaction"]["proposal"] is None  # consumed
    assert body["proposed_rule"] is None  # only two votes


async def test_field_present_merge_body_tags_keep_proposal_category(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    await _review(client, (await _inbox_txn(client))["id"], {"category_id": coffee})
    await _commit_csv(client, account_id, rows=[("2026-07-02", "-6.00", "STARBUCKS 123")])
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"tags": ["morning"]})
    body = r.json()
    assert body["result"] == "corrected"  # tags diverge from the (tagless) proposal
    assert body["transaction"]["category"]["id"] == coffee  # merged from proposal
    assert [t["name"] for t in body["transaction"]["tags"]] == ["morning"]


async def test_tags_are_casefold_deduped_and_logged_as_applied(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"tags": ["Coffee", "coffee ", "MORNING"]})
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()["transaction"]["tags"]] == ["Coffee", "MORNING"]
    entries = (await client.get(LOG, params={"transaction_id": txn["id"]})).json()["items"]
    assert entries[0]["decision_tags"] == ["Coffee", "MORNING"]


async def test_review_before_the_sweep_snapshots_provenance_none(client) -> None:
    """A missing proposal is legal (PRD): the pipeline never ran."""
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    # No run_jobs: the proposal does not exist yet.
    txn = await _inbox_txn(client)
    assert txn["proposal"] is None
    r = await _review(client, txn["id"], {"category_id": coffee})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "corrected"
    entries = (await client.get(LOG, params={"transaction_id": txn["id"]})).json()["items"]
    assert entries[0]["proposal_provenance"] == "none"
    assert entries[0]["decision_category_id"] == coffee


async def test_display_name_body_vs_proposal(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"display_name": "Starbucks"})
    body = r.json()
    assert body["result"] == "corrected"
    assert body["transaction"]["display_name"] == "Starbucks"


async def test_already_reviewed_answers_409(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    assert (await _review(client, txn["id"])).status_code == 200
    assert (await _review(client, txn["id"])).status_code == 409


async def test_unknown_category_404s_and_reviews_nothing(client, run_jobs) -> None:
    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    r = await _review(client, txn["id"], {"category_id": str(_uuid.uuid7())})
    assert r.status_code == 404
    assert (await client.get(f"{TX}/{txn['id']}")).json()["reviewed_at"] is None


async def test_tenancy_404(client, run_jobs) -> None:
    await _signup(client, email="a@example.com")
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    txn = await _inbox_txn(client)
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))
    await _signup(client, email="b@example.com")
    assert (await _review(client, txn["id"])).status_code == 404


async def test_third_consistent_review_proposes_a_rule(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    rule = None
    for i, day in enumerate(("01", "02", "03")):
        await _commit_csv(
            client, account_id, rows=[(f"2026-07-{day}", "-5.00", "STARBUCKS 123")]
        )
        await run_jobs()
        txn = await _inbox_txn(client)
        r = await _review(client, txn["id"], {"category_id": coffee})
        assert r.status_code == 200, r.text
        rule = r.json()["proposed_rule"]
        if i < 2:
            assert rule is None
    assert rule is not None
    assert rule["status"] == "proposed"
    assert rule["condition"]["payee"] == {"op": "equals", "value": "starbucks 123"}
    assert rule["action_category"]["id"] == coffee
    listed = (await client.get(RULES, params={"status": "proposed"})).json()["items"]
    assert [x["id"] for x in listed] == [rule["id"]]
```

Also add a read-scope test (mirror test_pat_api.py's `_mint` helper):
a PAT with `scopes=["read"]` gets 403 on `POST {TX}/{id}/review` (any uuid —
the scope guard fires before routing resolves the row).

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_reviews_api.py -x -q`
Expected: FAIL — 404s (route not registered).

- [ ] **Step 5: Write `api/reviews.py`**

```python
"""POST /api/v1/transactions/{id}/review and /transactions/review (M5 CP4,
#22): the human half of the flywheel. The body carries the FINAL user data;
the server diffs against the proposal to record accepted-vs-corrected;
empty body accepts as-is. Wraps CP3's consume_proposal and runs the inline
promotion check. Never accept-by-filter: reviewing data the user never saw
is not review.

Own Router (same /api/v1/transactions path as transactions_router) so this
module can import rules/transactions helpers without a cycle."""

import uuid
from typing import Annotated, Literal

from litestar import Router, post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath
from litestar.status_codes import HTTP_200_OK, HTTP_409_CONFLICT
from pydantic import BaseModel, ConfigDict, Field

from pinch_backend.api.rules import RuleOut, rule_out
from pinch_backend.api.transactions import TransactionOut, hydrate_transactions
from pinch_backend.classification.consume import consume_proposal
from pinch_backend.classification.promotion import maybe_propose_rule
from pinch_backend.models import (
    Category,
    CorrectionActor,
    Ledger,
    Proposal,
    ProposalTag,
    Transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.tags import dedupe_tag_names

log = get_logger(__name__)


class ReviewIn(BaseModel):
    """The FINAL user data. Field-present merge against the proposal: an
    absent field means "the proposal's value", a present one is the user's
    final word. Empty body accepts as-is. notes is not reviewable — that is
    PATCH's job, and clearing display_name likewise (consume applies
    display_name only when not None)."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    category_id: uuid.UUID | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=100)


class ReviewOut(BaseModel):
    """The review envelope: the consent moment rides the response — a
    just-minted proposed rule is shown right here, never polled for."""

    transaction: TransactionOut
    result: Literal["accepted", "corrected"]
    proposed_rule: RuleOut | None


async def _pending_proposal(txn_id: uuid.UUID) -> tuple[Proposal | None, list[str]]:
    proposal = await Proposal.where(lambda p, tid=txn_id: p.transaction_id == tid).first()
    if proposal is None:
        return None, []
    proposal_id = proposal.id
    names = [
        pt.name
        for pt in await ProposalTag.where(lambda pt, pid=proposal_id: pt.proposal_id == pid)
        .order_by(lambda pt: pt.id)
        .all()
    ]
    return proposal, names


@post("/{txn_id:uuid}/review", status_code=HTTP_200_OK)
async def review_transaction(
    txn_id: FromPath[uuid.UUID],
    current_ledger: NamedDependency[Ledger],
    data: ReviewIn | None = None,
) -> ReviewOut:
    ledger_id = current_ledger.id
    txn = await Transaction.where(
        lambda t, tid=txn_id, lid=ledger_id: (t.id == tid) & (t.ledger_id == lid)
    ).first()
    if txn is None:
        raise NotFoundException(detail="No such transaction")
    if txn.reviewed_at is not None:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Already reviewed; un-review first (PATCH reviewed: false)",
        )

    body = data if data is not None else ReviewIn()
    fields = data.model_fields_set if data is not None else set()

    if "category_id" in fields and body.category_id is not None:
        wanted = body.category_id
        category = await Category.where(
            lambda c, cid=wanted, lid=ledger_id: (c.id == cid) & (c.ledger_id == lid)
        ).first()
        if category is None:
            raise NotFoundException(detail="No such category")

    proposal, proposal_tags = await _pending_proposal(txn.id)
    prop_category_id = proposal.category_id if proposal else None  # ty: ignore[unresolved-attribute]
    prop_display = proposal.proposed_display_name if proposal else None

    final_category = body.category_id if "category_id" in fields else prop_category_id
    final_tags = dedupe_tag_names(
        list(body.tags or []) if "tags" in fields else proposal_tags
    )
    final_display = body.display_name if "display_name" in fields else prop_display

    corrected = (
        final_category != prop_category_id
        or {t.casefold() for t in final_tags} != {t.casefold() for t in proposal_tags}
        or (final_display is not None and final_display != prop_display)
    )

    await consume_proposal(
        current_ledger,
        txn,
        category_id=final_category,
        tags=final_tags,
        display_name=final_display,
        actor=CorrectionActor.USER,
    )
    rule = await maybe_propose_rule(current_ledger, txn.description_normalized, final_category)

    result: Literal["accepted", "corrected"] = "corrected" if corrected else "accepted"
    log.info(
        "review.corrected" if corrected else "review.accepted",
        transaction_id=str(txn.id),
        ledger_id=str(ledger_id),
        promoted_rule_id=str(rule.id) if rule else None,
    )
    (out,) = await hydrate_transactions([txn])
    return ReviewOut(
        transaction=out,
        result=result,
        proposed_rule=await rule_out(rule) if rule else None,
    )


reviews_router = Router(path="/api/v1/transactions", route_handlers=[review_transaction])
```

Register in `api/app.py`: import `reviews_router` from
`pinch_backend.api.reviews`, add to `route_handlers` right after
`transactions_router`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_reviews_api.py tests/test_rules_api.py tests/test_transactions_api.py -q`
Expected: all PASS. If Litestar rejects the optional body (`data: ReviewIn | None = None`), fall back to a required body and have tests always send `json={}` — "empty body" means `{}` on the wire either way; note the deviation in the task report.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src tests && uv run ty check
git add -A src tests
git commit -m "feat(api): single-transaction review endpoint with inline promotion (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Batch review endpoint

**Files:**
- Modify: `src/pinch_backend/api/reviews.py`
- Test: `tests/test_reviews_api.py` (append)

**Interfaces:**
- Consumes: `_pending_proposal`, `dedupe_tag_names`, `consume_proposal`, `maybe_propose_rule`, `rule_out` (Tasks 1–2).
- Produces: `POST /api/v1/transactions/review` — `ReviewBatchIn {ids}`, `ReviewBatchOut {accepted, skipped, proposed_rules}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_reviews_api.py`)

```python
async def _batch(client, ids: list[str]):
    return await client.post(f"{TX}/review", json={"ids": ids}, headers=await _csrf(client))


async def test_batch_counts_are_honest_and_idempotent(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(
        client,
        account_id,
        rows=[
            ("2026-07-01", "-5.00", "STARBUCKS 123"),
            ("2026-07-02", "-42.00", "MYSTERY CO"),
            ("2026-07-03", "-7.00", "PEETS"),
        ],
    )
    await run_jobs()
    ids = [t["id"] for t in await _transactions(client)]
    await _review(client, ids[0])  # one already reviewed
    r = await _batch(client, ids)
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 2, "skipped": 1, "proposed_rules": []}
    again = await _batch(client, ids)
    assert again.json() == {"accepted": 0, "skipped": 3, "proposed_rules": []}
    assert all(t["reviewed_at"] is not None for t in await _transactions(client))


async def test_batch_unknown_id_404s_and_consumes_nothing(client, run_jobs) -> None:
    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id)
    await run_jobs()
    ids = [t["id"] for t in await _transactions(client)]
    ghost = str(_uuid.uuid7())
    r = await _batch(client, [*ids, ghost])
    assert r.status_code == 404
    assert ghost in str(r.json())
    assert all(t["reviewed_at"] is None for t in await _transactions(client))


async def test_batch_duplicate_ids_count_once(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    (txn,) = await _transactions(client)
    r = await _batch(client, [txn["id"], txn["id"]])
    assert r.json()["accepted"] == 1
    assert r.json()["skipped"] == 0


async def test_batch_cap_1000(client) -> None:
    import uuid as _uuid

    await _signup(client)
    r = await _batch(client, [str(_uuid.uuid7()) for _ in range(1001)])
    assert r.status_code == 400


async def test_batch_accepting_a_third_history_proposal_promotes(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    await _commit_csv(client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")])
    await run_jobs()
    await _review(client, (await _inbox_txn(client))["id"], {"category_id": coffee})
    await _commit_csv(
        client,
        account_id,
        rows=[("2026-07-02", "-6.00", "STARBUCKS 123"), ("2026-07-03", "-7.00", "STARBUCKS 123")],
    )
    await run_jobs()
    pending = [t["id"] for t in await _transactions(client) if t["reviewed_at"] is None]
    r = await _batch(client, pending)
    body = r.json()
    assert body["accepted"] == 2
    assert len(body["proposed_rules"]) == 1  # one check per distinct payee
    assert body["proposed_rules"][0]["condition"]["payee"]["value"] == "starbucks 123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reviews_api.py -q -k batch`
Expected: FAIL — 404 (route not registered) / 405.

- [ ] **Step 3: Implement** (append to `api/reviews.py`; add `review_batch` to the Router's route_handlers)

```python
class ReviewBatchIn(BaseModel):
    """Explicit ids only (<=1,000 clears a realistic month) — never
    accept-by-filter. Duplicates are deduped preserving order."""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class ReviewBatchOut(BaseModel):
    accepted: int
    skipped: int
    proposed_rules: list[RuleOut]


@post("/review", status_code=HTTP_200_OK)
async def review_batch(
    data: ReviewBatchIn, current_ledger: NamedDependency[Ledger]
) -> ReviewBatchOut:
    ledger_id = current_ledger.id
    ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for wanted in data.ids:
        if wanted not in seen:
            seen.add(wanted)
            ids.append(wanted)

    txns = await Transaction.where(
        lambda t, wanted=ids, lid=ledger_id: (t.ledger_id == lid) & (t.id.in_(wanted))
    ).all()
    by_id = {t.id: t for t in txns}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        # Validate-all-first: skipped means "already reviewed", never
        # "silently didn't exist" — a stale or foreign id fails loudly.
        raise NotFoundException(
            detail="Unknown transactions in batch", extra={"missing_ids": missing}
        )

    accepted = skipped = 0
    decided: dict[str, uuid.UUID | None] = {}
    for wanted in ids:
        txn = by_id[wanted]
        if txn.reviewed_at is not None:
            skipped += 1
            continue
        proposal, proposal_tags = await _pending_proposal(txn.id)
        final_category = proposal.category_id if proposal else None  # ty: ignore[unresolved-attribute]
        await consume_proposal(
            current_ledger,
            txn,
            category_id=final_category,
            tags=dedupe_tag_names(proposal_tags),
            display_name=proposal.proposed_display_name if proposal else None,
            actor=CorrectionActor.USER,
        )
        accepted += 1
        decided[txn.description_normalized] = final_category

    proposed: list[RuleOut] = []
    for payee, category_id in decided.items():
        rule = await maybe_propose_rule(current_ledger, payee, category_id)
        if rule is not None:
            proposed.append(await rule_out(rule))
    log.info(
        "review.batch_completed",
        ledger_id=str(ledger_id),
        accepted=accepted,
        skipped=skipped,
        rules_proposed=len(proposed),
    )
    return ReviewBatchOut(accepted=accepted, skipped=skipped, proposed_rules=proposed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reviews_api.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ty check
git add src/pinch_backend/api/reviews.py tests/test_reviews_api.py
git commit -m "feat(api): batch review — explicit ids, honest counts, idempotent skips (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: PATCH integration — reviewing consumes, un-reviewing defers

**Files:**
- Modify: `src/pinch_backend/api/transactions.py` (`patch_transaction` + one helper)
- Test: `tests/test_transactions_api.py` (append)

**Interfaces:**
- Consumes: `consume_proposal`, `maybe_propose_rule`, `dedupe_tag_names`, `classify_ledger` (jobs.py).
- Produces: the invariant every later test relies on — *setting reviewed always consumes and logs*; `PATCH reviewed: false` enqueues `classification.classify_ledger`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_transactions_api.py`; reuse that file's existing helpers for signup/account/import — read them first, they differ slightly from test_classification_api.py)

Assert through GET /transactions, GET /api/v1/correction-log, and the `job_connector` fixture. `LOG = "/api/v1/correction-log"`, `RULES = "/api/v1/rules"`; `_patch(client, txn_id, body)` = `client.patch(f"{TX}/{txn_id}", json=body, headers=await _csrf(client))`; `_log_entries(client, txn_id)` = `(await client.get(LOG, params={"transaction_id": txn_id})).json()["items"]`; a small `_seeded_inbox_txn(client, account_id, *, day="01")` helper that commits one `STARBUCKS 123` row for that day, runs jobs, and returns the unreviewed transaction dict:

```python
async def test_patch_reviewed_true_consumes_the_pending_proposal(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    txn = await _seeded_inbox_txn(client, account_id)
    assert txn["proposal"] is not None
    r = await _patch(client, txn["id"], {"reviewed": True})
    assert r.status_code == 200, r.text
    after = (await client.get(f"{TX}/{txn['id']}")).json()
    assert after["reviewed_at"] is not None
    assert after["proposal"] is None  # consumed, not left attached (CP3 wart closed)
    entries = await _log_entries(client, txn["id"])
    assert len(entries) == 1
    assert entries[0]["kind"] == "decision"
    assert entries[0]["actor"] == "user"
    assert entries[0]["proposal_provenance"] == "none"  # nothing matched this payee
    assert entries[0]["decision_category_id"] is None  # the user set nothing


async def test_patch_category_plus_reviewed_logs_the_final_state(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    txn = await _seeded_inbox_txn(client, account_id)
    r = await _patch(client, txn["id"], {"category_id": coffee, "reviewed": True})
    assert r.status_code == 200, r.text
    assert r.json()["category"]["id"] == coffee
    entries = await _log_entries(client, txn["id"])
    assert entries[0]["decision_category_id"] == coffee


async def test_three_patch_reviews_of_a_payee_promote(client, run_jobs) -> None:
    """PATCH-review appends user-actor decisions: promotion evidence."""
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    for day in ("01", "02", "03"):
        txn = await _seeded_inbox_txn(client, account_id, day=day)
        await _patch(client, txn["id"], {"category_id": coffee, "reviewed": True})
    proposed = (await client.get(RULES, params={"status": "proposed"})).json()["items"]
    assert len(proposed) == 1
    assert proposed[0]["condition"]["payee"] == {"op": "equals", "value": "starbucks 123"}


async def test_patch_reviewed_true_without_tags_keeps_existing_tags_in_the_log(
    client, run_jobs
) -> None:
    await _signup(client)
    account_id = await _account(client)
    txn = await _seeded_inbox_txn(client, account_id)
    await _patch(client, txn["id"], {"tags": ["morning"]})
    await _patch(client, txn["id"], {"reviewed": True})
    entries = await _log_entries(client, txn["id"])
    assert entries[0]["decision_tags"] == ["morning"]  # the current set, not []


async def test_unreview_defers_a_sweep_and_the_roundtrip_appends(
    client, run_jobs, job_connector
) -> None:
    await _signup(client)
    account_id = await _account(client)
    txn = await _seeded_inbox_txn(client, account_id)
    await _review(client, txn["id"])
    assert len(await _log_entries(client, txn["id"])) == 1
    before = len(job_connector.jobs)
    r = await _patch(client, txn["id"], {"reviewed": False})
    assert r.status_code == 200, r.text
    assert len(job_connector.jobs) == before + 1  # un-review defers the sweep
    await run_jobs()
    reproposed = (await client.get(f"{TX}/{txn['id']}")).json()
    assert reproposed["reviewed_at"] is None
    assert reproposed["proposal"] is not None  # the sweep re-proposed
    await _review(client, txn["id"])
    entries = await _log_entries(client, txn["id"])
    assert len(entries) == 2  # re-review appends; earlier entries stand


async def test_noop_reviewed_transitions_neither_defer_nor_log(
    client, run_jobs, job_connector
) -> None:
    await _signup(client)
    account_id = await _account(client)
    txn = await _seeded_inbox_txn(client, account_id)
    before = len(job_connector.jobs)
    await _patch(client, txn["id"], {"reviewed": False})  # already unreviewed
    assert len(job_connector.jobs) == before  # no defer
    await _patch(client, txn["id"], {"reviewed": True})
    await _patch(client, txn["id"], {"reviewed": True})  # already reviewed
    assert len(await _log_entries(client, txn["id"])) == 1  # exactly one decision
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transactions_api.py -q -k "patch_reviewed or unreview or noop_reviewed"`
Expected: FAIL — proposal still attached after PATCH, no log entries, no job.

- [ ] **Step 3: Implement in `patch_transaction`**

New imports in `api/transactions.py`: `consume_proposal`, `maybe_propose_rule`, `dedupe_tag_names`, `classify_ledger` (from `pinch_backend.jobs`), `CorrectionActor`. Add the helper:

```python
async def _current_tag_names(txn: Transaction) -> list[str]:
    txn_id = txn.id
    links = await TransactionTag.where(lambda tt, tid=txn_id: tt.transaction_id == tid).all()
    tag_ids = sorted({link.tag_id for link in links})  # ty: ignore[unresolved-attribute]
    if not tag_ids:
        return []
    rows = await Tag.where(lambda t, ids=tag_ids: t.id.in_(ids)).all()
    return sorted((t.name for t in rows), key=str.casefold)
```

Replace the `async with transaction():` block of `patch_transaction` with:

```python
    reviewing = "reviewed" in fields and data.reviewed is True and txn.reviewed_at is None
    unreviewing = "reviewed" in fields and data.reviewed is False and txn.reviewed_at is not None

    if reviewing:
        # Setting reviewed always consumes and logs (M5 CP4): the pending
        # proposal is consumed with the transaction's post-PATCH state as
        # the decision — the final state IS the decision. consume_proposal
        # saves the row, so in-memory mutations ride its transaction.
        if "category_id" in fields:
            txn.category_id = category_id  # ty: ignore[unresolved-attribute]
        if "display_name" in fields:
            txn.display_name = data.display_name
        if "notes" in fields:
            txn.notes = data.notes
        final_tags = (
            dedupe_tag_names(list(data.tags or []))
            if "tags" in fields
            else await _current_tag_names(txn)
        )
        await consume_proposal(
            current_ledger,
            txn,
            category_id=txn.category_id,  # ty: ignore[unresolved-attribute]
            tags=final_tags,
            display_name=txn.display_name,
            actor=CorrectionActor.USER,
        )
        await maybe_propose_rule(
            current_ledger,
            txn.description_normalized,
            txn.category_id,  # ty: ignore[unresolved-attribute]
        )
    else:
        async with transaction():
            if "category_id" in fields:
                txn.category_id = category_id  # ty: ignore[unresolved-attribute]
            if "display_name" in fields:
                txn.display_name = data.display_name
            if "notes" in fields:
                txn.notes = data.notes
            if "reviewed" in fields:
                txn.reviewed_at = utcnow() if data.reviewed else None
            await txn.save()
            if "tags" in fields:
                await apply_tag_set(current_ledger, txn, data.tags or [])
        if unreviewing:
            # Deferred AFTER the transaction commits (the imports.py
            # precedent) so the round-trip is prompt: un-review -> sweep
            # re-proposes -> re-review appends; earlier entries stand.
            await classify_ledger.configure(lock=f"ledger:{current_ledger.id}").defer_async(
                ledger_id=str(current_ledger.id), auto_file_import_id=None
            )
```

(`display_name` present-and-null with `reviewed: true`: the in-memory None
rides consume's `txn.save()`; consume's `display_name` arg is then None so
apply is a no-op and the log's `decision_display_name` is null — correct:
no display decision was made.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transactions_api.py tests/test_reviews_api.py tests/test_classification_api.py -q`
Expected: all PASS. If any existing test pinned the old wart (PATCH
`reviewed: true` leaving the proposal attached — `grep -rn "reviewed.*[Tt]rue" tests/ | grep -i proposal` to find candidates), UPDATE it to assert the
new consume semantics, citing the spec's "setting reviewed always consumes
and logs" decision in the test docstring.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ty check
git add src/pinch_backend/api/transactions.py tests/
git commit -m "feat(api): PATCH reviewed:true consumes; reviewed:false defers a sweep (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Manual transaction entry

**Files:**
- Modify: `src/pinch_backend/api/transactions.py` (add `TransactionCreateIn` + `create_transaction`, register on `transactions_router`)
- Create: `tests/test_manual_entry_api.py`

**Interfaces:**
- Consumes: `compute_fingerprint`, `normalize_description` (imports/fingerprint.py); `consume_proposal`, `maybe_propose_rule`, `dedupe_tag_names`, `classify_ledger`; `Account`, `Connection` (models.py).
- Produces: `POST /api/v1/transactions` (201, `TransactionOut`).

- [ ] **Step 1: Write the failing tests**

`tests/test_manual_entry_api.py` (helpers as in test_reviews_api.py; `LOG`, `TX`, `RULES` constants):

```python
"""POST /api/v1/transactions — manual entry (M5 CP4, #22): manual accounts
only; without category an ordinary incoming transaction (sweep, inbox);
with category/tags reviewed at birth (empty-proposal log entry, actor=user).
Fingerprint via the M4 recipe so later CSV overlaps flag."""


async def _manual(client, account_id: str, body: dict | None = None):
    payload = {
        "account_id": account_id,
        "date": "2026-07-10",
        "amount_minor": -1250,
        "description": "Farmers Market",
    } | (body or {})
    return await client.post(TX, json=payload, headers=await _csrf(client))


async def test_uncategorized_manual_entry_is_ordinary_incoming(client, run_jobs, job_connector):
    await _signup(client)
    account_id = await _account(client)
    before = len(job_connector.jobs)
    r = await _manual(client, account_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reviewed_at"] is None
    assert body["currency"] == "USD"  # the account's, never the payload's
    assert len(job_connector.jobs) == before + 1  # manual creation enqueues
    await run_jobs()
    txn = (await client.get(f"{TX}/{body['id']}")).json()
    assert txn["proposal"] is not None  # the sweep classified it
    assert (await client.get(LOG, params={"transaction_id": body["id"]})).json()["items"] == []


async def test_categorized_manual_entry_is_reviewed_at_birth(client, job_connector):
    await _signup(client)
    account_id = await _account(client)
    groceries = await _category(client, "Groceries")
    before = len(job_connector.jobs)
    r = await _manual(client, account_id, {"category_id": groceries, "tags": ["market"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reviewed_at"] is not None
    assert body["category"]["id"] == groceries
    assert [t["name"] for t in body["tags"]] == ["market"]
    assert body["proposal"] is None
    assert len(job_connector.jobs) == before  # born reviewed: no sweep needed
    entries = (await client.get(LOG, params={"transaction_id": body["id"]})).json()["items"]
    assert len(entries) == 1
    assert entries[0]["actor"] == "user"
    assert entries[0]["proposal_provenance"] == "none"  # the pipeline never ran
    assert entries[0]["decision_category_id"] == groceries


async def test_tags_alone_review_at_birth_but_annotations_do_not(client):
    await _signup(client)
    account_id = await _account(client)
    tagged = await _manual(client, account_id, {"tags": ["cash"]})
    assert tagged.json()["reviewed_at"] is not None
    annotated = await _manual(
        client,
        account_id,
        {"date": "2026-07-11", "display_name": "Market", "notes": "cash run"},
    )
    body = annotated.json()
    assert body["reviewed_at"] is None  # annotations are not decisions
    assert body["display_name"] == "Market"
    assert body["notes"] == "cash run"


async def test_connected_account_answers_409(client, db):
    from pinch_backend.models import Account, Connection, Ledger

    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    account = await Account.get(_uuid.UUID(account_id))
    ledger = await Ledger.get(account.ledger_id)  # ty: ignore[unresolved-attribute]
    connection = await Connection.create(ledger=ledger, provider_item_id="item-1")
    connected = await Account.create(
        ledger=ledger, kind=account.kind, label="Linked", currency="USD", connection=connection
    )
    r = await _manual(client, str(connected.id))
    assert r.status_code == 409


async def test_account_tenancy_and_category_404(client):
    import uuid as _uuid

    await _signup(client)
    account_id = await _account(client)
    assert (await _manual(client, str(_uuid.uuid7()))).status_code == 404
    r = await _manual(client, account_id, {"category_id": str(_uuid.uuid7())})
    assert r.status_code == 404


async def test_later_csv_overlap_flags_against_the_hand_entered_row(client, run_jobs):
    await _signup(client)
    account_id = await _account(client)
    r = await _manual(client, account_id)  # 2026-07-10, -1250, Farmers Market
    assert r.status_code == 201
    # The same movement arrives in a CSV: the fingerprint collides, the row
    # is flagged, and default commit skips it (M4 semantics).
    await _commit_csv(client, account_id, rows=[("2026-07-10", "-12.50", "Farmers Market")])
    await run_jobs()
    matching = [
        t
        for t in await _transactions(client)
        if t["description_raw"] == "Farmers Market"
    ]
    assert len(matching) == 1  # the duplicate was skipped at commit


async def test_three_manual_filings_promote(client):
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    for day in ("01", "02", "03"):
        r = await _manual(
            client,
            account_id,
            {"date": f"2026-07-{day}", "description": "Blue Bottle", "category_id": coffee},
        )
        assert r.status_code == 201
    proposed = (await client.get(RULES, params={"status": "proposed"})).json()["items"]
    assert len(proposed) == 1
    assert proposed[0]["condition"]["payee"] == {"op": "equals", "value": "blue bottle"}
```

Plus a read-scope test: read-only PAT gets 403 on `POST /api/v1/transactions`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_manual_entry_api.py -x -q`
Expected: FAIL — 405 (no POST handler on the route).

- [ ] **Step 3: Implement `create_transaction`**

In `api/transactions.py` (new imports: `Account`, `HTTPException`, `HTTP_201_CREATED`/`HTTP_409_CONFLICT` from litestar.status_codes, `post` from litestar, `compute_fingerprint` + `normalize_description` from `pinch_backend.imports.fingerprint`):

```python
class TransactionCreateIn(BaseModel):
    """Manual entry (M5 CP4): source fields + the full optional user-data
    set. Manual accounts only; the currency is always the account's. With
    category or tags the transaction is reviewed at birth; display_name and
    notes alone are annotations, not decisions."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    account_id: uuid.UUID
    date: date
    amount_minor: int
    """Signed from the account's perspective — negative is money out."""
    description: str = Field(min_length=1, max_length=500)
    category_id: uuid.UUID | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=50
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


@post("/")
async def create_transaction(
    data: TransactionCreateIn, current_ledger: NamedDependency[Ledger]
) -> TransactionOut:
    ledger_id = current_ledger.id
    wanted_account = data.account_id
    account = await Account.where(
        lambda a, aid=wanted_account, lid=ledger_id: (a.id == aid) & (a.ledger_id == lid)
    ).first()
    if account is None:
        raise NotFoundException(detail="No such account")
    if account.connection_id is not None:  # ty: ignore[unresolved-attribute]
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail="Manual entry is for manual accounts"
        )
    if data.category_id is not None:
        wanted = data.category_id
        category = await Category.where(
            lambda c, cid=wanted, lid=ledger_id: (c.id == cid) & (c.ledger_id == lid)
        ).first()
        if category is None:
            raise NotFoundException(detail="No such category")

    decided = data.category_id is not None or bool(data.tags)
    tags = dedupe_tag_names(list(data.tags or []))
    async with transaction():
        txn = await Transaction.create(
            ledger=current_ledger,
            account=account,
            date=data.date,
            amount_minor=data.amount_minor,
            currency=account.currency,
            description_raw=data.description,
            description_normalized=normalize_description(data.description),
            fingerprint=compute_fingerprint(
                account.id, data.date, data.amount_minor, data.description
            ),
            display_name=data.display_name,
            notes=data.notes,
        )
        if decided:
            # Reviewed at birth: no proposal exists, so consume snapshots
            # provenance=none — the pipeline never ran. Nested transaction
            # is atomic with the outer (scratch-verified): no observable
            # state where the row exists categorized-but-unlogged.
            await consume_proposal(
                current_ledger,
                txn,
                category_id=data.category_id,
                tags=tags,
                display_name=data.display_name,
                actor=CorrectionActor.USER,
            )
    rule = None
    if decided:
        rule = await maybe_propose_rule(current_ledger, txn.description_normalized, data.category_id)
    else:
        await classify_ledger.configure(lock=f"ledger:{ledger_id}").defer_async(
            ledger_id=str(ledger_id), auto_file_import_id=None
        )
    log.info(
        "transaction.created",
        transaction_id=str(txn.id),
        ledger_id=str(ledger_id),
        reviewed=decided,
        promoted_rule_id=str(rule.id) if rule else None,
    )
    (out,) = await hydrate_transactions([txn])
    return out
```

Add `create_transaction` to `transactions_router`'s `route_handlers`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_manual_entry_api.py tests/test_transactions_api.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ty check
git add src/pinch_backend/api/transactions.py tests/test_manual_entry_api.py
git commit -m "feat(api): manual transaction entry — both birth paths (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Delete-vs-dismiss semantics + CP3 coverage debts

**Files:**
- Modify: `src/pinch_backend/api/rules.py` (delete_rule docstring only)
- Test: `tests/test_reviews_api.py`, `tests/test_correction_log_api.py`, `tests/test_classification_api.py` (append)

**Interfaces:** consumes everything already shipped; produces no new surface.

- [ ] **Step 1: Update `delete_rule`'s docstring**

Document: deleting is "forget this ever happened" — deleting a proposed or
dismissed rule erases the promotion tombstone, so re-proposal is possible by
design (and deleting a dismissed rule is the only undo for a fat-fingered
dismiss). Dismissing (`PATCH status: dismissed`) is "never ask again".

- [ ] **Step 2: Write the failing/new tests**

In `tests/test_reviews_api.py` (a `_arrive(client, account_id, day)` helper commits one `STARBUCKS 123` row and runs jobs — same shape as Task 7's `arrive`; `_review` from Task 2):

```python
async def test_dismissed_tombstone_blocks_but_delete_reopens(client, run_jobs) -> None:
    """Dismiss = never again; delete = forget, re-proposal possible."""
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")
    rule = None
    for day in ("01", "02", "03"):
        txn = await _arrive(client, account_id, day)
        r = await _review(client, txn["id"], {"category_id": coffee})
        rule = r.json()["proposed_rule"]
    assert rule is not None
    dismissed = await client.patch(
        f"{RULES}/{rule['id']}", json={"status": "dismissed"}, headers=await _csrf(client)
    )
    assert dismissed.status_code == 200, dismissed.text
    txn = await _arrive(client, account_id, "04")
    r = await _review(client, txn["id"], {"category_id": coffee})
    assert r.json()["proposed_rule"] is None  # the tombstone covers the payee
    deleted = await client.delete(f"{RULES}/{rule['id']}", headers=await _csrf(client))
    assert deleted.status_code == 204, deleted.text
    txn = await _arrive(client, account_id, "05")
    r = await _review(client, txn["id"], {"category_id": coffee})
    fresh = r.json()["proposed_rule"]
    assert fresh is not None  # the ledger forgot: a fresh mint
    assert fresh["id"] != rule["id"]


async def test_consume_leaves_notes_untouched(client, run_jobs) -> None:
    """Non-vacuous (CP3 debt): notes carries a real value through review."""
    await _signup(client)
    account_id = await _account(client)
    txn = await _arrive(client, account_id, "01")
    await client.patch(
        f"{TX}/{txn['id']}", json={"notes": "check this"}, headers=await _csrf(client)
    )
    assert (await _review(client, txn["id"])).status_code == 200
    after = (await client.get(f"{TX}/{txn['id']}")).json()
    assert after["notes"] == "check this"
    assert after["reviewed_at"] is not None
```

In `tests/test_classification_api.py` (its own helpers: `_signup`, `_account`, `_commit_csv`, `_category`, `_rule`, `_transactions`):

```python
async def test_auto_filed_decisions_feed_history(client, run_jobs) -> None:
    """CP3 debt: an auto-filed transaction is a legitimate history source."""
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee Z")
    await _rule(client, contains="starbucks", category_id=coffee)
    await _commit_csv(
        client, account_id, rows=[("2026-07-01", "-5.00", "STARBUCKS 123")], auto_file=True
    )
    await run_jobs()
    (auto_filed,) = await _transactions(client)
    assert auto_filed["reviewed_at"] is not None
    assert auto_filed["category"]["id"] == coffee
    # Remove the rule: history is now the only possible category source.
    rules = (await client.get(RULES)).json()["items"]
    deleted = await client.delete(f"{RULES}/{rules[0]['id']}", headers=await _csrf(client))
    assert deleted.status_code == 204, deleted.text
    await _commit_csv(client, account_id, rows=[("2026-07-02", "-6.00", "STARBUCKS 123")])
    await run_jobs()
    fresh = [t for t in await _transactions(client) if t["reviewed_at"] is None]
    assert fresh[0]["proposal"]["provenance"] == "history"
    assert fresh[0]["proposal"]["category"]["id"] == coffee
```

(`RULES = "/api/v1/rules"` — add the constant if the file lacks it.)

In `tests/test_correction_log_api.py` (reuse that file's existing setup helpers — read it first; it already drives decisions through the model or HTTP seam):

```python
async def test_multi_page_cursor_walk(client, run_jobs) -> None:
    """CP3 debt: the id-keyset cursor walks this endpoint page by page."""
    await _signup(client)
    account_id = await _account(client)
    txn_ids = []
    for day in ("01", "02", "03"):
        txn = await _arrive(client, account_id, day)
        await _review(client, txn["id"])
        txn_ids.append(txn["id"])
    first = (await client.get(LOG, params={"limit": 2})).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    second = (
        await client.get(LOG, params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    seen = {e["id"] for e in first["items"]} | {e["id"] for e in second["items"]}
    assert len(seen) == 3  # no overlap, nothing dropped


async def test_combined_filters(client, run_jobs) -> None:
    """CP3 debt: transaction_id + actor + kind compose (AND semantics)."""
    await _signup(client)
    account_id = await _account(client)
    txn = await _arrive(client, account_id, "01")
    other = await _arrive(client, account_id, "02")
    await _review(client, txn["id"])
    await _review(client, other["id"])
    params = {"transaction_id": txn["id"], "actor": "user", "kind": "decision"}
    items = (await client.get(LOG, params=params)).json()["items"]
    assert [e["transaction_id"] for e in items] == [txn["id"]]
    none = (await client.get(LOG, params=params | {"actor": "auto"})).json()["items"]
    assert none == []
```

(Both need the `_arrive`/`_review`/`_signup`/`_account` helper shapes —
import-or-copy per that file's local convention; `_arrive` days give each
transaction a distinct fingerprint.)

- [ ] **Step 3: Run the new tests**

Run: `uv run pytest tests/test_reviews_api.py tests/test_correction_log_api.py tests/test_classification_api.py -q`
Expected: the delete-reopens and debt tests PASS against the shipped code
(they pin behavior, not drive it — if any FAILS, that is a real defect:
stop and fix before committing).

- [ ] **Step 4: Lint + commit**

```bash
uv run ruff check src tests && uv run ty check
git add src/pinch_backend/api/rules.py tests/
git commit -m "test(api): delete-vs-dismiss pin + CP3 coverage debts closed (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: The flywheel end-to-end test

**Files:**
- Create: `tests/test_flywheel_e2e.py`

**Interfaces:** consumes the entire CP1–CP4 HTTP surface; produces the milestone's acceptance test.

- [ ] **Step 1: Write the test** (helpers as in test_reviews_api.py)

```python
"""The M5 thesis, end to end at the HTTP seam (#22): import -> propose ->
correct -> history learns -> third consistent filing -> proposed rule ->
accept -> rule wins precedence. One test, the whole flywheel."""


async def test_the_flywheel(client, run_jobs) -> None:
    await _signup(client)
    account_id = await _account(client)
    coffee = await _category(client, "Coffee")

    async def arrive(day: str, amount: str) -> dict:
        await _commit_csv(client, account_id, rows=[(f"2026-07-{day}", amount, "STARBUCKS 123")])
        await run_jobs()
        return await _inbox_txn(client)

    # 1. First arrival: every stage abstains — the empty proposal.
    txn = await arrive("01", "-5.00")
    assert txn["proposal"]["provenance"] == "none"
    assert txn["proposal"]["category"] is None
    # 2. The user corrects. One vote; no rule yet.
    r = await _review(client, txn["id"], {"category_id": coffee})
    assert r.json()["result"] == "corrected"
    assert r.json()["proposed_rule"] is None
    # 3. Second arrival: history learned the correction.
    txn = await arrive("02", "-6.00")
    assert txn["proposal"]["provenance"] == "history"
    assert txn["proposal"]["category"]["id"] == coffee
    r = await _review(client, txn["id"])
    assert r.json()["result"] == "accepted"
    assert r.json()["proposed_rule"] is None  # two votes
    # 4. Third consistent filing: the rule is proposed — consent asked.
    txn = await arrive("03", "-7.00")
    r = await _review(client, txn["id"])
    rule = r.json()["proposed_rule"]
    assert rule is not None
    assert rule["status"] == "proposed"
    assert rule["condition"]["payee"] == {"op": "equals", "value": "starbucks 123"}
    assert rule["action_category"]["id"] == coffee
    # 5. Proposed is not law: the next arrival is still history-proposed.
    txn = await arrive("04", "-8.00")
    assert txn["proposal"]["provenance"] == "history"
    r = await _review(client, txn["id"])
    assert r.json()["proposed_rule"] is None  # the covering rule blocks re-mint
    # 6. The user consents: one status flip.
    accepted = await client.patch(
        f"{RULES}/{rule['id']}", json={"status": "active"}, headers=await _csrf(client)
    )
    assert accepted.status_code == 200, accepted.text
    # 7. The rule wins precedence over history.
    txn = await arrive("05", "-9.00")
    assert txn["proposal"]["provenance"] == "rule"
    assert txn["proposal"]["category"]["id"] == coffee
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_flywheel_e2e.py -q`
Expected: PASS against the shipped code. A failure here is a real
integration defect — debug it (systematic-debugging), never weaken the
assertions.

- [ ] **Step 3: Commit**

```bash
git add tests/test_flywheel_e2e.py
git commit -m "test(e2e): the full flywheel over the HTTP seam (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Gate — full suite, PR #23 body, progress ledger

**Files:**
- Modify: `.superpowers/sdd/progress.md` (CP4 section)
- Modify: PR #23 body (via `gh pr edit`)

- [ ] **Step 1: The gate**

```bash
uv run pytest -q            # expect: baseline 331 + every new test, 0 failures
uv run ruff check src tests
uv run ty check
uv run prek run --all-files
```

All green or fix before proceeding (verification-before-completion).

- [ ] **Step 2: Update PR #23**

`gh pr view 23 --json body -q .body > /tmp/pr23.md`, then edit: tick the
CP4 checkbox (`- [x] **CP4 (#22)** — review, promotion, manual entry`), and
append a `## CP4 — the human half (added)` section in the established CP1–
CP3 style covering: the two review endpoints (envelope responses, honest
counts, validate-all-first), *setting reviewed always consumes and logs*
(the CP3 wart closed), un-review defers the sweep, the promotion engine
(evidence rules, tombstones, delete-vs-dismiss), manual entry (both birth
paths, account currency, M4 fingerprint), the flywheel e2e test, CP3
coverage debts closed, and the final test count. `gh pr edit 23 --body-file /tmp/pr23.md`.

- [ ] **Step 3: Update `.superpowers/sdd/progress.md`** with the CP4 task ledger (per-task one-liners, minors for final-review triage), commit both:

```bash
git add .superpowers/sdd/progress.md
git commit -m "docs(sdd): CP4 execution ledger (M5 CP4, #22)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

Do NOT push — the final whole-branch review gates the push (the CP1–CP3 workflow).

---

## Execution notes for the controller

- Task order is strict: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 (each consumes the previous surface).
- Per-task: fresh subagent, TDD, then spec+quality review before the next task (subagent-driven-development).
- Known review hot-spots from CP1–CP3: the co-author trailer (verify verbatim per commit), B023 bindings in new `where()` lambdas, `# ty: ignore[unresolved-attribute]` scope on shadow-FK reads, and TYPE_CHECKING placement of annotation-only imports.
