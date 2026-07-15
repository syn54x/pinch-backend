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
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger
from pinch_backend.tags import apply_tag_set

if TYPE_CHECKING:
    import uuid

log = get_logger(__name__)


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
