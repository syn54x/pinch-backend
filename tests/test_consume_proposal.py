"""The shared consume-proposal operation (M5 CP3, #21): claim -> log ->
apply -> proposal deleted, one transaction. CP4 exposes it over HTTP."""

import uuid
from datetime import date

import pytest

from pinch_backend.classification.consume import AlreadyReviewedError, consume_proposal
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
    utcnow,
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
        ledger=ledger,
        transaction=txn,
        category=coffee,
        provenance=ProposalProvenance.HISTORY,
        provenance_detail={"matched_transaction_id": "m-1"},
        proposed_display_name="Starbucks",
    )
    await ProposalTag.create(ledger=ledger, proposal=proposal, name="treat")

    entry = await consume_proposal(
        ledger,
        txn,
        category_id=coffee.id,
        tags=["treat"],
        display_name="Starbucks",
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
        ledger=ledger,
        transaction=txn,
        category=coffee,
        provenance=ProposalProvenance.RULE,
        provenance_detail={"rule_ids": ["r-1"]},
    )
    entry = await consume_proposal(
        ledger,
        txn,
        category_id=dining.id,
        tags=[],
        display_name=None,
        actor=CorrectionActor.USER,
    )
    assert entry.proposal_category_id == coffee.id
    assert entry.decision_category_id == dining.id
    assert (await Transaction.get(txn.id)).category_id == dining.id
    assert (await Transaction.get(txn.id)).display_name is None  # None = leave alone


async def test_consume_without_proposal_snapshots_none(db) -> None:
    ledger, txn = await _seed(db)
    entry = await consume_proposal(
        ledger,
        txn,
        category_id=None,
        tags=[],
        display_name=None,
        actor=CorrectionActor.USER,
    )
    assert entry.proposal_provenance is ProposalProvenance.NONE
    assert entry.proposal_category_id is None
    assert entry.decision_category_id is None  # accept-as-uncategorized is legal
    assert (await Transaction.get(txn.id)).reviewed_at is not None


async def test_consume_losing_to_a_concurrent_review_raises_and_writes_nothing(db) -> None:
    """The finding-4 race, deterministically interleaved: the caller (say,
    auto-file) holds a stale unreviewed `txn` — its caller-side freshness
    check already passed — while a human review commits category + stamp.
    Only consume's in-transaction CAS claim can refuse. Pre-fix, this call
    overwrote the user's category, re-stamped reviewed_at, and appended an
    AUTO decision superseding the user's vote in promotion evidence."""
    ledger, txn = await _seed(db)
    coffee = await Category.create(ledger=ledger, name="Coffee Y")
    dining = await Category.create(ledger=ledger, name="Dining Y")
    await Proposal.create(
        ledger=ledger,
        transaction=txn,
        category=coffee,
        provenance=ProposalProvenance.HISTORY,
        proposed_display_name="Starbucks",
    )
    # The human's review commits AFTER `txn` was fetched: the in-memory row
    # still says reviewed_at=None, exactly like auto-file's batch copy.
    now = utcnow()
    await Transaction.where(lambda t, tid=txn.id: t.id == tid).update(
        reviewed_at=now, category_id=dining.id, updated_at=now
    )

    with pytest.raises(AlreadyReviewedError):
        await consume_proposal(
            ledger,
            txn,
            category_id=coffee.id,
            tags=["treat"],
            display_name="Starbucks",
            actor=CorrectionActor.AUTO,
        )

    fresh = await Transaction.get(txn.id)
    assert fresh.category_id == dining.id  # the user's category stands
    assert fresh.reviewed_at == now  # not re-stamped
    assert fresh.display_name is None  # the rename was not applied
    assert (
        await CorrectionLogEntry.where(lambda e, tid=txn.id: e.transaction_id == tid).count() == 0
    )  # no AUTO decision superseding the user's vote
    assert await Proposal.where(lambda p, tid=txn.id: p.transaction_id == tid).count() == 1


async def test_second_consume_of_the_same_transaction_raises(db) -> None:
    """Double-consume (the POST review double-click, past the 409 pre-check):
    the second claim finds reviewed_at set and raises — one decision entry,
    the first stamp untouched."""
    ledger, txn = await _seed(db)
    await consume_proposal(
        ledger, txn, category_id=None, tags=[], display_name=None, actor=CorrectionActor.USER
    )
    first_stamp = (await Transaction.get(txn.id)).reviewed_at

    with pytest.raises(AlreadyReviewedError):
        await consume_proposal(
            ledger, txn, category_id=None, tags=[], display_name=None, actor=CorrectionActor.USER
        )

    assert (
        await CorrectionLogEntry.where(lambda e, tid=txn.id: e.transaction_id == tid).count() == 1
    )
    assert (await Transaction.get(txn.id)).reviewed_at == first_stamp
