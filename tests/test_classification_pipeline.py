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
    await _txn(
        ledger, account, "starbucks", reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=dining
    )
    # ...but an active rule says coffee, and rules are law.
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "equals", "value": "starbucks"}},
        action_category=coffee,
    )
    ruled = await _txn(ledger, account, "starbucks")
    history_only = await _txn(ledger, account, "blue bottle")
    await _txn(
        ledger,
        account,
        "blue bottle",
        reviewed_at=datetime(2026, 7, 1, tzinfo=UTC),
        category=coffee,
    )
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
    await _txn(
        ledger, account, "starbucks", reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee
    )
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
        action_category=coffee,
        action_add_tags=["a"],
        action_rename_to="First",
    )
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "bucks"}},
        action_category=dining,
        action_add_tags=["b", "A"],
        action_rename_to="Second",
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
            ledger=ledger,
            status=status,
            condition={"version": 1, "payee": {"op": "equals", "value": "starbucks"}},
            action_category=coffee,
        )
    txn = await _txn(ledger, account, "starbucks")
    await sweep_ledger(ledger.id)
    assert (await _proposal_for(txn)).provenance is ProposalProvenance.NONE


async def test_sweep_is_idempotent_and_skips_reviewed(db) -> None:
    ledger, account = await _seed(db)
    plain = await _txn(ledger, account, "mystery co")
    reviewed = await _txn(
        ledger, account, "decided inc", reviewed_at=datetime(2026, 7, 1, tzinfo=UTC)
    )
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
    await _txn(
        ledger, account, "starbucks", reviewed_at=datetime(2026, 7, 1, tzinfo=UTC), category=coffee
    )
    batch = await Import.create(
        ledger=ledger,
        account=account,
        status=ImportStatus.COMMITTED,
        filename="backfill.csv",
        file_bytes=b"",
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
