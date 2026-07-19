"""The transfer detector (M7 CP4, issue #36; PRD #31).

A post-classification pass, not a pipeline stage: rules/history/AI see one
transaction; the detector's whole point is seeing two. Every ingestion
path — sync, import commit, manual creation — funnels through the classify
job, so running here covers them all.

The candidate rule wraps the M6 model invariants (opposite signs, equal
magnitudes, same currency, same ledger, different accounts, neither split
nor linked, nonzero) plus the one thing that is heuristic and never a
model invariant: a ±5-day date window (settlement lag is real; the number
is tunable, not sacred).

**Mutual uniqueness or silence**: a proposal is written only when each
side's sole eligible match is the other. A wrong link is worse than a
missed one — rent-sized recurring amounts make ambiguity common, and
manual linking is always one POST away. Reviewed transactions count as
candidates (the Thursday case: checking reviewed Monday, card side posts
Thursday) but only unreviewed sides *receive* proposals — accepting the
unreviewed side later vacates the reviewed counterpart's category, per the
relaxed M6 semantics.

Overwriting is deliberate: a matched pair outranks whatever category or
untracked-transfer shape the pipeline proposed — the detector simply knows
more. Contributed tags and renames ride along (a `cc-payment` tag on a
transfer is legitimate, M6)."""

import datetime
import uuid
from collections import defaultdict

from pinch_backend.models import (
    CorrectionKind,
    CorrectionLogEntry,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    SplitLine,
    Transaction,
    Transfer,
)
from pinch_backend.observability import get_logger
from pinch_backend.retraction import voided_decision_ids

log = get_logger(__name__)

DETECTION_WINDOW_DAYS = 5
"""The heuristic half-window: |date difference| <= this many days."""


async def detect_transfers(ledger_id: uuid.UUID) -> int:
    """Write mirrored detection proposals for mutually-unique pairs.
    Idempotent per sweep: an existing detection proposal naming the same
    counterpart is left untouched. Returns proposals written."""
    txns = await Transaction.where(
        lambda t, lid=ledger_id: (t.ledger_id == lid) & (t.amount_minor != 0)
    ).all()
    if not txns:
        return 0
    txn_ids = [t.id for t in txns]
    split_ids = {
        ln.transaction_id  # ty: ignore[unresolved-attribute]
        for ln in await SplitLine.where(lambda ln, ids=txn_ids: ln.transaction_id.in_(ids)).all()
    }
    linked_ids: set[uuid.UUID] = set()
    for tr in await Transfer.where(
        lambda tr, ids=txn_ids: (
            (tr.outflow_transaction_id.in_(ids)) | (tr.inflow_transaction_id.in_(ids))
        )
    ).all():
        for member in (tr.outflow_transaction_id, tr.inflow_transaction_id):  # ty: ignore[unresolved-attribute]
            if member is not None:
                linked_ids.add(member)

    eligible = [t for t in txns if t.id not in split_ids and t.id not in linked_ids]
    by_magnitude: dict[tuple[str, int], list[Transaction]] = defaultdict(list)
    for t in eligible:
        by_magnitude[(t.currency, abs(t.amount_minor))].append(t)

    window = datetime.timedelta(days=DETECTION_WINDOW_DAYS)

    def matches(a: Transaction, b: Transaction) -> bool:
        return (
            (a.amount_minor < 0) != (b.amount_minor < 0)
            and a.account_id != b.account_id  # ty: ignore[unresolved-attribute]
            and abs(a.date - b.date) <= window
        )

    rejected = await _rejected_pairs(ledger_id)
    pairs: list[tuple[Transaction, Transaction]] = []
    for group in by_magnitude.values():
        if len(group) < 2:
            continue
        for t in group:
            if t.amount_minor >= 0:
                continue  # walk each pair once, from the outflow side
            candidates = [c for c in group if matches(t, c)]
            if len(candidates) != 1:
                continue
            (c,) = candidates
            # Mutual uniqueness: the counterpart's sole match must be t.
            if len([x for x in group if matches(c, x)]) != 1:
                continue
            if frozenset((t.id, c.id)) in rejected:
                continue  # the user already declined this pairing; a
                # changed mind arrives via POST /transfers or an undo-void
            pairs.append((t, c))

    written = 0
    for a, b in pairs:
        for side, other in ((a, b), (b, a)):
            if side.reviewed_at is not None:
                continue  # reviewed rows aren't in the inbox; no proposal to carry
            written += await _propose_detection(ledger_id, side, other)
    if written:
        log.info("detection.proposed", ledger_id=str(ledger_id), proposals=written)
    return written


async def _rejected_pairs(ledger_id: uuid.UUID) -> set[frozenset]:
    """Pairings a user has already declined: a non-voided decision whose
    proposal was detection-shaped but whose outcome wasn't the link. The
    correction log is the rejection memory — self-contained snapshots, so
    this survives everything (and an undo-void honestly re-arms the pair)."""
    decisions = await CorrectionLogEntry.where(
        lambda e, lid=ledger_id: (
            (e.ledger_id == lid)
            & (e.kind == CorrectionKind.DECISION)
            & (e.proposal_provenance == ProposalProvenance.DETECTION)
        )
    ).all()
    voided = await voided_decision_ids([d.id for d in decisions])
    rejected: set[frozenset] = set()
    for d in decisions:
        if d.id in voided:
            continue
        if d.decision_transfer is not None and d.decision_transfer.get("kind") == "linked":
            continue  # accepted, not rejected
        declined = (
            d.decision_category_id is not None
            or d.decision_splits is not None
            or (d.decision_transfer is not None and d.decision_transfer.get("kind") == "untracked")
        )
        if not declined:
            # An all-empty outcome is a DEGRADED accept (the counterpart
            # turned ineligible between detection and consent), not a
            # decline — the user said yes; the pair stays re-proposable
            # should eligibility return. Every genuine rejection carries a
            # positive alternative (category, splits, or untracked).
            continue
        counterpart = (d.proposal_detail or {}).get("counterpart_transaction_id")
        if counterpart:
            rejected.add(frozenset((d.transaction_id, uuid.UUID(counterpart))))
    return rejected


async def _propose_detection(
    ledger_id: uuid.UUID, side: Transaction, counterpart: Transaction
) -> int:
    """Replace ``side``'s proposal with the detection shape, carrying over
    contributed tags and rename. No-op when already exactly this."""
    side_id = side.id
    existing = await Proposal.where(lambda p, tid=side_id: p.transaction_id == tid).first()
    carried_tags: list[str] = []
    carried_display: str | None = None
    if existing is not None:
        if (
            existing.provenance == ProposalProvenance.DETECTION
            and existing.counterpart_transaction_id == counterpart.id  # ty: ignore[unresolved-attribute]
        ):
            return 0  # idempotent sweep
        existing_id = existing.id
        carried_tags = [
            pt.name
            for pt in await ProposalTag.where(lambda pt, pid=existing_id: pt.proposal_id == pid)
            .order_by(lambda pt: pt.id)
            .all()
        ]
        carried_display = existing.proposed_display_name
        await ProposalTag.where(lambda pt, pid=existing_id: pt.proposal_id == pid).delete()
        await existing.delete()
    proposal = await Proposal.create(
        ledger_id=ledger_id,
        transaction=side,
        category=None,
        proposed_display_name=carried_display,
        proposed_transfer=True,
        counterpart_transaction=counterpart,
        provenance=ProposalProvenance.DETECTION,
        provenance_detail={"counterpart_transaction_id": str(counterpart.id)},
    )
    for name in carried_tags:
        await ProposalTag.create(ledger_id=ledger_id, proposal=proposal, name=name)
    return 1
