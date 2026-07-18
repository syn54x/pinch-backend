"""Consuming a proposal into user data + the correction log (PRD M5 D13):
claim -> log entry -> apply -> delete the Proposal, one database
transaction. CP3's auto-file calls this; CP4's review endpoints wrap it.

The unreviewed -> reviewed transition guard lives HERE: the first write in
the transaction is an atomic compare-and-set claim on ``reviewed_at IS NULL``
(the api/imports.py commit-CAS precedent), so every caller inherits it. A
concurrent decision makes the claim come back empty and consume raises
:class:`AlreadyReviewedError` instead of writing over the winner — callers'
own reviewed_at checks are fast paths and noise reduction, never the guard.
The mirror residual (a sweep inserting a proposal onto a just-reviewed row)
is accepted and documented at pipeline.py's phase-1 freshness comment.

The caller supplies the FINAL user data (auto-file passes the proposal's own
values; review passes the user's, possibly corrected). Tag rows are minted
here — "created implicitly on first use" means the user's data actually
carries the tag. display_name is applied only when not None: clearing an
override is PATCH's job, not review's. A missing proposal is legal (manual
entry, review before the sweep ran — CP4): the snapshot records
provenance=none, the pipeline never ran.
"""

from typing import TYPE_CHECKING

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
    SplitLine,
    Transaction,
    Transfer,
    utcnow,
)
from pinch_backend.observability import get_logger
from pinch_backend.tags import apply_tag_set

if TYPE_CHECKING:
    import uuid

log = get_logger(__name__)


class AlreadyReviewedError(Exception):
    """The in-transaction claim found the transaction already reviewed — a
    concurrent decision won. Callers translate: the review endpoints answer
    409, the batch counts a skip, auto-file moves to the next row."""


async def _split_transfer_state(
    txn_id: "uuid.UUID",
) -> tuple[list[dict] | None, dict | None]:
    """The transaction's split/transfer state as log-ready snapshots (M6
    CP3): names-not-FKs, ids as strings. Read inside consume's transaction so
    the snapshot and the claim see one version of the row's world."""
    lines = (
        await SplitLine.where(lambda ln, tid=txn_id: ln.transaction_id == tid)
        .order_by(lambda ln: ln.id)
        .all()
    )
    decision_splits = None
    if lines:
        line_cat_ids = sorted(
            {ln.category_id for ln in lines if ln.category_id is not None}  # ty: ignore[unresolved-attribute]
        )
        line_names = (
            {
                c.id: c.name
                for c in await Category.where(lambda c, ids=line_cat_ids: c.id.in_(ids)).all()
            }
            if line_cat_ids
            else {}
        )
        decision_splits = [
            {
                "amount_minor": ln.amount_minor,
                "category_id": str(ln.category_id) if ln.category_id else None,  # ty: ignore[unresolved-attribute]
                "category_name": line_names.get(ln.category_id),  # ty: ignore[unresolved-attribute]
                "memo": ln.memo,
            }
            for ln in lines
        ]

    transfer = await Transfer.where(
        lambda tr, tid=txn_id: (
            (tr.outflow_transaction_id == tid) | (tr.inflow_transaction_id == tid)
        )
    ).first()
    decision_transfer = None
    if transfer is not None:
        counterpart_id = (
            transfer.inflow_transaction_id  # ty: ignore[unresolved-attribute]
            if transfer.outflow_transaction_id == txn_id  # ty: ignore[unresolved-attribute]
            else transfer.outflow_transaction_id  # ty: ignore[unresolved-attribute]
        )
        counterpart_account_id = None
        if counterpart_id is not None:
            counterpart = await Transaction.where(lambda t, cid=counterpart_id: t.id == cid).first()
            if counterpart is not None:
                counterpart_account_id = counterpart.account_id  # ty: ignore[unresolved-attribute]
        decision_transfer = {
            "kind": "linked" if counterpart_id is not None else "untracked",
            "counterpart_transaction_id": str(counterpart_id) if counterpart_id else None,
            "counterpart_account_id": (
                str(counterpart_account_id) if counterpart_account_id else None
            ),
        }
    return decision_splits, decision_transfer


async def consume_proposal(
    ledger: Ledger,
    txn: Transaction,
    *,
    category_id: "uuid.UUID | None",
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
            for pt in await ProposalTag.where(lambda pt, pid=proposal_id: pt.proposal_id == pid)
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
        # Atomic claim (CAS): the UPDATE locks the row and re-evaluates the
        # predicate against its current version under READ COMMITTED, so —
        # unlike the pre-transaction fetches above or any caller-side check —
        # it cannot pass on a stale reviewed_at. Zero rows claimed means a
        # concurrent decision already reviewed this transaction: nothing
        # below may run over it.
        stamp = utcnow()
        claimed = await Transaction.where(
            lambda t, tid=txn_id: (t.id == tid) & (t.reviewed_at == None)  # noqa: E711
        ).update(reviewed_at=stamp, updated_at=stamp)
        if claimed == 0:
            raise AlreadyReviewedError(f"transaction {txn_id} is already reviewed")
        # Split/transfer awareness (M6 CP3): when the transaction ends up
        # split or in a transfer, the category layer is not this row's to
        # hold — the caller's category is not applied, and the decision is
        # logged as what it actually was, never a fake "uncategorized" shrug.
        decision_splits, decision_transfer = await _split_transfer_state(txn_id)
        if decision_splits is not None or decision_transfer is not None:
            category_id = None
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
            decision_splits=decision_splits,
            decision_transfer=decision_transfer,
        )
        txn.category_id = category_id  # ty: ignore[unresolved-attribute]
        if display_name is not None:
            txn.display_name = display_name
        # The claim already stamped the row; the full-row save below must
        # re-write the same stamp, not a second utcnow() or a stale value.
        txn.reviewed_at = stamp
        txn.updated_at = stamp
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
