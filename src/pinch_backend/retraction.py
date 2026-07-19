"""The retraction & dissolution seam (M6 contract; extracted at M7 CP3).

Import undo and sync-removed are the same event from different origins: a
transaction the ledger held no longer exists. One seam holds the contract:

- Transfers touching a retracted transaction dissolve; a surviving linked
  counterpart is REOPENED — a silently-restored-to-spending row is report
  pollution — and its transfer decision entry voided: the decision was made
  against a transaction that no longer exists (CONTEXT.md's void principle).
- Proposals die with their transactions; correction-log decision entries
  are voided with a later entry, never deleted (append-only).

The amount-rewrite path (also CP3) reuses the dissolution half with an
empty exclusion set: nothing is deleted, so *both* members reopen and both
lose their transfer decisions — the link itself was built on the amount.

Callers own the surrounding database transaction and the post-commit
re-classification defer.
"""

from typing import TYPE_CHECKING

from pinch_backend.models import (
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Proposal,
    ProposalTag,
    Transaction,
    Transfer,
    utcnow,
)

if TYPE_CHECKING:
    import uuid


async def void_decisions(
    ledger_id: "uuid.UUID",
    txn_id: "uuid.UUID",
    *,
    actor: CorrectionActor,
    reason: str,
    transfer_only: bool = False,
) -> None:
    """Void every not-yet-voided DECISION entry on the transaction with a
    later entry — never an edit, never a delete."""
    decisions = (
        await CorrectionLogEntry.where(
            lambda e, tid=txn_id: (e.transaction_id == tid) & (e.kind == CorrectionKind.DECISION)
        )
        .order_by(lambda e: e.id)
        .all()
    )
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
        if transfer_only and decision.decision_transfer is None:
            continue
        await CorrectionLogEntry.create(
            ledger_id=ledger_id,
            transaction_id=decision.transaction_id,
            kind=CorrectionKind.VOID,
            actor=actor,
            voids=decision.id,
            void_reason=reason,
        )


async def dissolve_transfers_touching(
    ledger_id: "uuid.UUID",
    touched_ids: "list[uuid.UUID]",
    *,
    exclude_members: "set[uuid.UUID]",
    actor: CorrectionActor,
    counterpart_reason: str,
) -> int:
    """Dissolve every transfer with a member in ``touched_ids``. Members
    outside ``exclude_members`` (rows that live on) are reopened and their
    transfer decisions voided; excluded members are the caller's to handle
    (about to be deleted, with a full void of their own). Returns how many
    members were reopened."""
    reopened_total = 0
    affected = await Transfer.where(
        lambda tr, ids=touched_ids: (
            (tr.outflow_transaction_id.in_(ids)) | (tr.inflow_transaction_id.in_(ids))
        )
    ).all()
    for link in affected:
        members = (link.outflow_transaction_id, link.inflow_transaction_id)  # ty: ignore[unresolved-attribute]
        survivor_ids = [m for m in members if m is not None and m not in exclude_members]
        await link.delete()
        for survivor_id in survivor_ids:
            reopened_total += await Transaction.where(
                lambda t, sid=survivor_id: (t.id == sid) & (t.reviewed_at != None)  # noqa: E711
            ).update(reviewed_at=None, updated_at=utcnow())
            await void_decisions(
                ledger_id, survivor_id, actor=actor, reason=counterpart_reason, transfer_only=True
            )
    return reopened_total


async def delete_proposals_for(txn_ids: "list[uuid.UUID]") -> None:
    proposal_ids = [
        p.id for p in await Proposal.where(lambda p, ids=txn_ids: p.transaction_id.in_(ids)).all()
    ]
    if proposal_ids:
        await ProposalTag.where(lambda pt, ids=proposal_ids: pt.proposal_id.in_(ids)).delete()
        await Proposal.where(lambda p, ids=proposal_ids: p.id.in_(ids)).delete()


async def retract_transactions(
    ledger_id: "uuid.UUID",
    txn_ids: "list[uuid.UUID]",
    *,
    actor: CorrectionActor,
    decision_reason: str,
    counterpart_reason: str,
) -> int:
    """The full retraction: dissolve transfers (reopening + voiding linked
    survivors), delete proposals, void every decision on the doomed rows,
    delete the rows (split lines cascade at the database). Returns how many
    surviving counterparts were reopened."""
    if not txn_ids:
        return 0
    reopened = await dissolve_transfers_touching(
        ledger_id,
        txn_ids,
        exclude_members=set(txn_ids),
        actor=actor,
        counterpart_reason=counterpart_reason,
    )
    await delete_proposals_for(txn_ids)
    for txn_id in txn_ids:
        await void_decisions(ledger_id, txn_id, actor=actor, reason=decision_reason)
    await Transaction.where(lambda t, ids=txn_ids: t.id.in_(ids)).delete()
    return reopened
