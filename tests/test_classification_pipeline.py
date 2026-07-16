"""Sweep semantics + the precedence matrix at the model seam (M5 CP3, #21).
The HTTP-seam flywheel tests live in test_classification_api.py (Task 7)."""

import asyncio
import uuid
from datetime import UTC, date, datetime

from ferro import engines

from pinch_backend.classification import pipeline
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


async def test_reviewed_between_batch_fetch_and_write_is_not_proposed(db, monkeypatch) -> None:
    ledger, account = await _seed(db)
    txn = await _txn(ledger, account, "mystery co")

    real_classify = pipeline.classify_transaction

    async def classify_then_mark_reviewed(t, active_rules):
        # Simulate a human PATCH (reviewed: true) landing between the
        # sweep's batch fetch and this transaction's write — the race in
        # Finding 1. If the freshness re-check inside the write transaction
        # is missing, the sweep proposes over this decision.
        now = datetime(2026, 7, 1, tzinfo=UTC)
        await Transaction.where(lambda tt, tid=t.id: tt.id == tid).update(
            reviewed_at=now, updated_at=now
        )
        return await real_classify(t, active_rules)

    monkeypatch.setattr(pipeline, "classify_transaction", classify_then_mark_reviewed)

    await sweep_ledger(ledger.id)

    assert await _proposal_for(txn) is None  # a human decided; the sweep must not propose
    reviewed = await Transaction.get(txn.id)
    assert reviewed.reviewed_at is not None


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


async def test_auto_file_reverifies_reviewed_before_consuming(db, monkeypatch) -> None:
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "starbucks"}},
        action_category=coffee,
    )
    batch = await Import.create(
        ledger=ledger,
        account=account,
        status=ImportStatus.COMMITTED,
        filename="backfill.csv",
        file_bytes=b"",
    )
    # Order note: the auto-file loop walks by id (uuid7 = creation order),
    # so txn_a is created (and consumed) before txn_b — the first
    # consume_proposal call below is always txn_a's.
    txn_a = await _txn(ledger, account, "starbucks", source_import=batch)
    txn_b = await _txn(ledger, account, "starbucks", source_import=batch)

    await sweep_ledger(ledger.id)  # both carry proposals now, still unreviewed

    real_consume = pipeline.consume_proposal
    calls = 0

    async def consume_then_interleave_once(ledger_arg, txn_arg, **kwargs):
        # Simulate a human PATCH (reviewed: true) landing on txn_b while
        # txn_a's auto-file consume is in flight — the race in Finding 1,
        # deterministically interleaved instead of concurrent.
        nonlocal calls
        calls += 1
        if calls == 1:
            now = datetime(2026, 7, 1, tzinfo=UTC)
            await Transaction.where(lambda t, tid=txn_b.id: t.id == tid).update(
                reviewed_at=now, updated_at=now
            )
        return await real_consume(ledger_arg, txn_arg, **kwargs)

    monkeypatch.setattr(pipeline, "consume_proposal", consume_then_interleave_once)

    await sweep_ledger(ledger.id, auto_file_import_id=batch.id)

    filed_a = await Transaction.get(txn_a.id)
    assert filed_a.reviewed_at is not None
    assert await _proposal_for(txn_a) is None  # consumed
    entries_a = await CorrectionLogEntry.where(
        lambda e, tid=txn_a.id: e.transaction_id == tid
    ).all()
    assert len(entries_a) == 1
    assert entries_a[0].actor is CorrectionActor.AUTO

    # txn_b was reviewed mid-batch by the "human" — the freshness guard
    # must skip it, not clobber it.
    entry_b = await CorrectionLogEntry.where(
        lambda e, tid=txn_b.id: e.transaction_id == tid
    ).first()
    assert entry_b is None  # never consumed
    assert await _proposal_for(txn_b) is not None  # proposal row untouched
    unfiled_b = await Transaction.get(txn_b.id)
    assert unfiled_b.category_id is None  # not resurrected by the stale batch-time txn


async def test_auto_file_skips_a_row_reviewed_after_the_freshness_check(db, monkeypatch) -> None:
    """The CAS backstop behind the caller-side re-check (PR review finding
    4): the "human" review commits AFTER auto-file's freshness re-check
    passed — this seam marks the row reviewed just before delegating to the
    real consume, whose in-transaction claim raises AlreadyReviewedError.
    The sweep must treat that like the `fresh is None` skip: no crash, no
    overwrite, and it keeps filing the rest of the batch."""
    ledger, account = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee P")
    await Rule.create(
        ledger=ledger,
        condition={"version": 1, "payee": {"op": "contains", "value": "starbucks"}},
        action_category=coffee,
    )
    batch = await Import.create(
        ledger=ledger,
        account=account,
        status=ImportStatus.COMMITTED,
        filename="backfill.csv",
        file_bytes=b"",
    )
    # uuid7 walk order: txn_a is consumed first (see the order note above).
    txn_a = await _txn(ledger, account, "starbucks", source_import=batch)
    txn_b = await _txn(ledger, account, "starbucks", source_import=batch)

    await sweep_ledger(ledger.id)  # both carry proposals now, still unreviewed

    real_consume = pipeline.consume_proposal

    async def review_wins_then_delegate(ledger_arg, txn_arg, **kwargs):
        if txn_arg.id == txn_a.id:
            now = datetime(2026, 7, 1, tzinfo=UTC)
            await Transaction.where(lambda t, tid=txn_a.id: t.id == tid).update(
                reviewed_at=now, updated_at=now
            )
        return await real_consume(ledger_arg, txn_arg, **kwargs)

    monkeypatch.setattr(pipeline, "consume_proposal", review_wins_then_delegate)

    await sweep_ledger(ledger.id, auto_file_import_id=batch.id)

    # txn_a: the human won inside the last round trip — nothing written.
    assert (
        await CorrectionLogEntry.where(lambda e, tid=txn_a.id: e.transaction_id == tid).count() == 0
    )
    assert await _proposal_for(txn_a) is not None  # proposal row untouched
    assert (await Transaction.get(txn_a.id)).category_id is None  # not overwritten

    # txn_b: the sweep continued past the loss and filed it normally.
    filed_b = await Transaction.get(txn_b.id)
    assert filed_b.reviewed_at is not None
    assert filed_b.category_id == coffee.id
    assert await _proposal_for(txn_b) is None  # consumed
    entries_b = await CorrectionLogEntry.where(
        lambda e, tid=txn_b.id: e.transaction_id == tid
    ).all()
    assert len(entries_b) == 1
    assert entries_b[0].actor is CorrectionActor.AUTO
