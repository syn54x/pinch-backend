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


async def _eligible_counterpart(
    ledger: Ledger, txn: Transaction, counterpart_id: "uuid.UUID"
) -> Transaction | None:
    """The detection proposal's counterpart, re-validated at accept time
    against the M6 model invariants — None means the link degraded away
    (the counterpart was linked, split, rewritten, or removed since)."""
    counterpart = await Transaction.where(
        lambda t, cid=counterpart_id, lid=ledger.id: (t.id == cid) & (t.ledger_id == lid)
    ).first()
    if counterpart is None:
        return None
    if (
        (txn.amount_minor < 0) == (counterpart.amount_minor < 0)
        or abs(txn.amount_minor) != abs(counterpart.amount_minor)
        or txn.currency != counterpart.currency
        or txn.account_id == counterpart.account_id  # ty: ignore[unresolved-attribute]
    ):
        return None
    cid = counterpart.id
    if (
        await SplitLine.where(lambda ln, c=cid: ln.transaction_id == c).first() is not None
        or await Transfer.where(
            lambda tr, c=cid: (tr.outflow_transaction_id == c) | (tr.inflow_transaction_id == c)
        ).first()
        is not None
    ):
        return None
    return counterpart


async def log_transfer_decision_on_reviewed(ledger: Ledger, counterpart: Transaction) -> None:
    """A later decision entry on an already-reviewed counterpart: it had no
    pending proposal (provenance=none records that honestly); the decision
    is the fresh linked state its snapshot reads."""
    decision_splits, decision_transfer = await _split_transfer_state(counterpart.id)
    await CorrectionLogEntry.create(
        ledger=ledger,
        transaction_id=counterpart.id,
        kind=CorrectionKind.DECISION,
        actor=CorrectionActor.USER,
        input_description_raw=counterpart.description_raw,
        input_payee=counterpart.description_normalized,
        input_amount_minor=counterpart.amount_minor,
        input_currency=counterpart.currency,
        input_date=counterpart.date,
        input_account_id=counterpart.account_id,  # ty: ignore[unresolved-attribute]
        proposal_provenance=ProposalProvenance.NONE,
        decision_category_id=None,
        decision_tags=[],
        decision_splits=decision_splits,
        decision_transfer=decision_transfer,
    )


async def _invalidate_mirror(*, counterpart_id: "uuid.UUID", txn_id: "uuid.UUID") -> None:
    """Delete the counterpart's mirror proposal if it still names this
    transaction. Only an unconsumed mirror can exist (consume deletes on
    review), so this never touches a decided row's history."""
    mirror = await Proposal.where(
        lambda p, cid=counterpart_id, tid=txn_id: (
            (p.transaction_id == cid) & (p.counterpart_transaction_id == tid)
        )
    ).first()
    if mirror is None:
        return
    mirror_id = mirror.id
    await ProposalTag.where(lambda pt, pid=mirror_id: pt.proposal_id == pid).delete()
    await mirror.delete()


async def consume_proposal(
    ledger: Ledger,
    txn: Transaction,
    *,
    category_id: "uuid.UUID | None",
    tags: list[str],
    display_name: str | None,
    actor: CorrectionActor,
    apply_proposed_transfer: bool = False,
) -> CorrectionLogEntry:
    """``apply_proposed_transfer`` is the accept-as-is switch for a
    transfer-shaped proposal (M6 CP4): True on the review endpoints' accept
    paths and auto-file — consuming creates the one-sided Transfer; False
    where the caller's data IS the decision (PATCH review, or an explicit
    category/splits/transfer decision replacing the proposal)."""
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
        linked_counterpart: Transaction | None = None
        if (
            apply_proposed_transfer
            and proposal is not None
            and proposal.proposed_transfer
            and txn.amount_minor != 0
        ):
            # Consume respects exclusivity (M6 CP4): a since-split or
            # since-linked transaction is accepted WITHOUT a transfer — the
            # standing state wins, and the snapshot below records it. The
            # one-round-trip window between this check and the insert is the
            # same accepted TOCTOU class as pipeline.py's phase-1 note; the
            # unique FK indexes keep a race from ever double-linking.
            already_split = (
                await SplitLine.where(lambda ln, tid=txn_id: ln.transaction_id == tid).first()
                is not None
            )
            already_linked = (
                await Transfer.where(
                    lambda tr, tid=txn_id: (
                        (tr.outflow_transaction_id == tid) | (tr.inflow_transaction_id == tid)
                    )
                ).first()
                is not None
            )
            counterpart_wanted: uuid.UUID | None = proposal.counterpart_transaction_id  # ty: ignore[unresolved-attribute]
            if not already_split and not already_linked and counterpart_wanted is None:
                negative = txn.amount_minor < 0
                await Transfer.create(
                    ledger=ledger,
                    outflow_transaction_id=txn_id if negative else None,
                    inflow_transaction_id=None if negative else txn_id,
                )
            elif not already_split and not already_linked and counterpart_wanted is not None:
                # A detection proposal (M7 CP4): the linked create. The same
                # degradation stance as above — a counterpart that turned
                # ineligible since detection (linked, split, rewritten, gone)
                # means accept WITHOUT a transfer, never an error; the
                # snapshot records what actually happened.
                linked_counterpart = await _eligible_counterpart(ledger, txn, counterpart_wanted)
                if linked_counterpart is not None:
                    negative = txn.amount_minor < 0
                    outflow, inflow = (
                        (txn, linked_counterpart) if negative else (linked_counterpart, txn)
                    )
                    await Transfer.create(
                        ledger=ledger,
                        outflow_transaction_id=outflow.id,
                        inflow_transaction_id=inflow.id,
                    )
                    # In a transfer ⇒ category NULL, both sides. The txn's
                    # own vacating rides the snapshot logic below; the
                    # counterpart — possibly already reviewed — vacates here
                    # (the relaxed M6 semantics: link created, category
                    # vacated, reviewed state untouched).
                    linked_counterpart.category_id = None  # ty: ignore[unresolved-attribute]
                    await linked_counterpart.save()
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
        if linked_counterpart is not None:
            # One consent consumes both sides (M7 CP4). An unreviewed
            # counterpart is consumed with the link as its decision (its
            # snapshot reads the fresh Transfer); a reviewed one stays
            # reviewed and gets the transfer decision as a later entry —
            # "a changed mind is a later entry, never an edit".
            if linked_counterpart.reviewed_at is None:
                await consume_proposal(
                    ledger,
                    linked_counterpart,
                    category_id=None,
                    tags=[],
                    display_name=None,
                    actor=actor,
                    apply_proposed_transfer=False,
                )
            else:
                await log_transfer_decision_on_reviewed(ledger, linked_counterpart)
        elif proposal is not None and proposal.counterpart_transaction_id is not None:  # ty: ignore[unresolved-attribute]
            # The linked interpretation did not happen — the user decided
            # otherwise, or the counterpart turned ineligible. The mirror on
            # the other side is now a trap (accepting it would vacate a
            # category the user just chose) and dies here; the caller
            # re-classifies its owner.
            await _invalidate_mirror(
                counterpart_id=proposal.counterpart_transaction_id,  # ty: ignore[unresolved-attribute]
                txn_id=txn_id,
            )
    log.info(
        "proposal.consumed",
        transaction_id=str(txn.id),
        ledger_id=str(ledger.id),
        actor=actor.value,
        entry_id=str(entry.id),
    )
    return entry
