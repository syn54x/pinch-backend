"""The classification sweep (PRD M5 D9/D13): idempotent, per-ledger,
background-only. Classify every unreviewed, proposal-less transaction —
active rules (creation order) -> exact payee history -> the classifier seam
-> the empty proposal. Precedence is per action type; provenance names the
CATEGORY's source. The unique transaction FK on Proposal is the concurrency
guard: of two racing sweeps, one insert wins and the loser skips.

This module is the ONE site that orders rules (uuid7 creation order) — the
explicit-priority door stays open (D13).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ferro import UniqueViolationError, transaction

from pinch_backend.classification.classifier import active_classifier
from pinch_backend.classification.consume import AlreadyReviewedError, consume_proposal
from pinch_backend.classification.history import history_match
from pinch_backend.models import (
    CorrectionActor,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Rule,
    RuleStatus,
    Transaction,
)
from pinch_backend.observability import get_logger
from pinch_backend.rules.evaluator import matches
from pinch_backend.rules.spec import ConditionSpec

if TYPE_CHECKING:
    import uuid

log = get_logger(__name__)

SWEEP_BATCH = 500
"""Keyset batch size for the sweep's transaction walk."""


@dataclass
class ProposalDraft:
    category_id: "uuid.UUID | None"
    provenance: ProposalProvenance
    detail: dict | None
    tag_names: list[str]
    display_name: str | None
    proposed_transfer: bool = False


async def classify_transaction(
    txn: Transaction, active_rules: list[tuple[Rule, ConditionSpec]]
) -> ProposalDraft:
    """Compose one transaction's draft. ``active_rules`` arrive pre-ordered
    (creation order); rules contribute tags/rename even when the category
    comes from a later stage — provenance_detail names every contributor."""
    matching = [(rule, spec) for rule, spec in active_rules if matches(spec, txn)]

    tag_names: list[str] = []
    seen_folds: set[str] = set()
    for rule, _ in matching:
        for name in rule.action_add_tags:
            fold = name.strip().casefold()
            if fold and fold not in seen_folds:
                seen_folds.add(fold)
                tag_names.append(name.strip())
    display_name = next(
        (rule.action_rename_to for rule, _ in matching if rule.action_rename_to), None
    )
    detail: dict = {}
    if matching:
        detail["rule_ids"] = [str(rule.id) for rule, _ in matching]

    # Precedence: transfer beats category (M6 CP4, the one new clause in the
    # per-action-type table) — any matching mark-transfer rule makes the
    # proposal transfer-shaped and suppresses history/AI. Zero-amount
    # transactions are unlinkable, so the clause never fires for them and
    # the category stages proceed.
    transfer_rule = next((rule for rule, _ in matching if rule.action_mark_transfer), None)
    if transfer_rule is not None and txn.amount_minor != 0:
        return ProposalDraft(
            category_id=None,
            provenance=ProposalProvenance.RULE,
            detail=detail,
            tag_names=tag_names,
            display_name=display_name,
            proposed_transfer=True,
        )

    category_rule = next(
        (rule for rule, _ in matching if rule.action_category_id is not None),  # ty: ignore[unresolved-attribute]
        None,
    )
    if category_rule is not None:
        return ProposalDraft(
            category_id=category_rule.action_category_id,  # ty: ignore[unresolved-attribute]
            provenance=ProposalProvenance.RULE,
            detail=detail,
            tag_names=tag_names,
            display_name=display_name,
        )

    hit = await history_match(txn.ledger_id, txn.description_normalized)  # ty: ignore[unresolved-attribute]
    # A hit with no category IS the untracked-transfer signal (see
    # history_match) — unproposable onto a zero-amount transaction.
    if hit is not None and (hit.category_id is not None or txn.amount_minor != 0):  # ty: ignore[unresolved-attribute]
        detail["matched_transaction_id"] = str(hit.id)
        return ProposalDraft(
            category_id=hit.category_id,  # ty: ignore[unresolved-attribute]
            provenance=ProposalProvenance.HISTORY,
            detail=detail,
            tag_names=tag_names,
            display_name=display_name,
            proposed_transfer=hit.category_id is None,  # ty: ignore[unresolved-attribute]
        )

    ai_category = await active_classifier.classify(txn)
    if ai_category is not None:
        return ProposalDraft(
            category_id=ai_category,
            provenance=ProposalProvenance.AI,
            detail=detail or None,
            tag_names=tag_names,
            display_name=display_name,
        )

    return ProposalDraft(
        category_id=None,
        provenance=ProposalProvenance.NONE,
        detail=detail or None,
        tag_names=tag_names,
        display_name=display_name,
    )


async def sweep_ledger(
    ledger_id: "uuid.UUID", *, auto_file_import_id: "uuid.UUID | None" = None
) -> None:
    """The idempotent sweep. Safe to run twice, safe to run concurrently,
    safe to crash and re-run: progress is the proposals themselves."""
    ledger = await Ledger.get(ledger_id)
    rules = (
        await Rule.where(
            lambda r, lid=ledger_id: (r.ledger_id == lid) & (r.status == RuleStatus.ACTIVE)
        )
        .order_by(lambda r: r.id)
        .all()
    )
    active_rules = [(rule, ConditionSpec(**rule.condition)) for rule in rules]

    written = 0
    last_id: "uuid.UUID | None" = None
    while True:
        query = Transaction.where(
            lambda t, lid=ledger_id: (t.ledger_id == lid) & (t.reviewed_at == None)  # noqa: E711
        )
        if last_id is not None:
            query = query.where(lambda t, after=last_id: t.id > after)
        batch = await query.order_by(lambda t: t.id).limit(SWEEP_BATCH).all()
        if not batch:
            break
        last_id = batch[-1].id
        batch_ids = [t.id for t in batch]
        proposed = {
            p.transaction_id  # ty: ignore[unresolved-attribute]
            for p in await Proposal.where(lambda p, ids=batch_ids: p.transaction_id.in_(ids)).all()
        }
        for txn in batch:
            if txn.id in proposed:
                continue
            draft = await classify_transaction(txn, active_rules)
            try:
                async with transaction():
                    # Freshness re-check: the batch was fetched before this
                    # await chain ran, so a human PATCH (reviewed: true) may
                    # have landed on this row in the meantime. Re-reading
                    # reviewed_at here, inside the same transaction as the
                    # write, shrinks that window to a single round trip under
                    # READ COMMITTED. The unique transaction FK is a separate
                    # guard — it only protects sweep-vs-sweep races, not a
                    # sweep racing a human decision.
                    #
                    # Accepted residual: a review can still commit inside that
                    # one remaining round trip (between this SELECT and the
                    # INSERT below), leaving an orphan proposal attached to a
                    # reviewed transaction — rendered until a later consume
                    # (review after un-review) deletes it. No lock closes this
                    # here by design; the decision itself is protected by
                    # consume_proposal's in-transaction CAS claim, so the
                    # orphan can never override or re-stamp a human decision.
                    txn_id = txn.id
                    fresh = await Transaction.where(
                        lambda t, tid=txn_id: (t.id == tid) & (t.reviewed_at == None)  # noqa: E711
                    ).first()
                    if fresh is None:
                        continue  # reviewed while this batch was in flight — a human decided
                    # Shadow-FK kwarg (category_id): runtime-synthesized.
                    # .create()'s **fields is untyped, so no ignore is needed
                    # here (unlike direct constructor calls, which is where
                    # this codebase's ty-ignores live).
                    proposal = await Proposal.create(
                        ledger=ledger,
                        transaction=txn,
                        category_id=draft.category_id,
                        proposed_display_name=draft.display_name,
                        proposed_transfer=draft.proposed_transfer,
                        provenance=draft.provenance,
                        provenance_detail=draft.detail,
                    )
                    for name in draft.tag_names:
                        await ProposalTag.create(ledger=ledger, proposal=proposal, name=name)
            except UniqueViolationError:
                continue  # a concurrent sweep won this transaction
            written += 1
            log.info(
                "proposal.written",
                transaction_id=str(txn.id),
                ledger_id=str(ledger_id),
                provenance=draft.provenance.value,
            )

    auto_filed = 0
    if auto_file_import_id is not None:
        import_id = auto_file_import_id
        last_id = None
        while True:
            query = Transaction.where(
                lambda t, iid=import_id: (t.source_import_id == iid) & (t.reviewed_at == None)  # noqa: E711
            )
            if last_id is not None:
                query = query.where(lambda t, after=last_id: t.id > after)
            batch = await query.order_by(lambda t: t.id).limit(SWEEP_BATCH).all()
            if not batch:
                break
            last_id = batch[-1].id
            for txn in batch:
                txn_id = txn.id
                proposal = await Proposal.where(
                    lambda p, tid=txn_id: p.transaction_id == tid
                ).first()
                if proposal is None:
                    # No proposal on an unreviewed row of this import: a
                    # concurrent human consume most likely took it (there is
                    # no auto-file retry) — skipping is the correct outcome.
                    continue
                proposal_id = proposal.id
                tag_names = [
                    pt.name
                    for pt in await ProposalTag.where(
                        lambda pt, pid=proposal_id: pt.proposal_id == pid
                    )
                    .order_by(lambda pt: pt.id)
                    .all()
                ]
                # Freshness re-check — noise reduction, not the guard: the
                # airtight unreviewed -> reviewed guard is consume_proposal's
                # in-transaction CAS claim (AlreadyReviewedError below). The
                # proposal/tag fetches just awaited past this batch's fetch,
                # so re-reading reviewed_at here skips rows a human already
                # decided without opening a doomed write transaction, and
                # passing `fresh` (not the stale `txn`) means consume's
                # whole-row save can't resurrect stale notes/display_name.
                fresh = await Transaction.where(
                    lambda t, tid=txn_id: (t.id == tid) & (t.reviewed_at == None)  # noqa: E711
                ).first()
                if fresh is None:
                    continue  # reviewed while this batch was in flight — a human decided
                try:
                    await consume_proposal(
                        ledger,
                        fresh,
                        category_id=proposal.category_id,  # ty: ignore[unresolved-attribute]
                        tags=tag_names,
                        display_name=proposal.proposed_display_name,
                        actor=CorrectionActor.AUTO,
                        apply_proposed_transfer=True,  # auto-file applies as-is (CP4)
                    )
                except AlreadyReviewedError:
                    # A human decided while consume was in flight — the same
                    # skip as `fresh is None`, one round trip later.
                    continue
                auto_filed += 1
        log.info(
            "import.auto_filed",
            import_id=str(auto_file_import_id),
            ledger_id=str(ledger_id),
            transactions=auto_filed,
        )

    log.info(
        "classification.sweep_completed",
        ledger_id=str(ledger_id),
        proposals_written=written,
        auto_filed=auto_filed,
    )
