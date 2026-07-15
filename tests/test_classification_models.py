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
