"""Retro-apply (F4 Enabler B, #67; CONTEXT.md: Retro-apply).

The shared half of the consent contract: split a condition's full match set
into what each tier would touch. Split parents and transfer members keep
their structure — one layer holds categories, and transfer exclusion derives
from membership — so they are *skipped*, and the counts say so honestly
before consent is given.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from pinch_backend.models import (
    Category,
    CorrectionActor,
    CorrectionKind,
    CorrectionLogEntry,
    Ledger,
    Proposal,
    ProposalProvenance,
    ProposalTag,
    Rule,
    SplitLine,
    Tag,
    Transaction,
    TransactionTag,
    Transfer,
)


class RetroApplyTier(StrEnum):
    """The escalating consent tiers, chosen at rule creation (CONTEXT.md:
    Retro-apply). Cumulative: UNREVIEWED includes forward; FULL includes
    both. Never re-offered on edit."""

    FORWARD = "forward"
    UNREVIEWED = "unreviewed"
    FULL = "full"


@dataclass
class MatchBreakdown:
    """The consent counts: what each retro-apply tier would touch."""

    unreviewed: list[Transaction] = field(default_factory=list)
    reviewed: list[Transaction] = field(default_factory=list)
    skipped: list[Transaction] = field(default_factory=list)


async def breakdown(matched: "list[Transaction]") -> MatchBreakdown:
    """Split a full match set by what retro-apply may touch. Two membership
    queries over the matched ids — never per-row."""
    out = MatchBreakdown()
    if not matched:
        return out
    ids: list[uuid.UUID] = [t.id for t in matched]
    split_parents = {
        ln.transaction_id  # ty: ignore[unresolved-attribute]
        for ln in await SplitLine.where(lambda ln, tids=ids: ln.transaction_id.in_(tids)).all()
    }
    transfer_members: set[uuid.UUID] = set()
    for tr in await Transfer.where(
        lambda tr, tids=ids: (
            tr.outflow_transaction_id.in_(tids) | tr.inflow_transaction_id.in_(tids)
        )
    ).all():
        if tr.outflow_transaction_id is not None:  # ty: ignore[unresolved-attribute]
            transfer_members.add(tr.outflow_transaction_id)  # ty: ignore[unresolved-attribute]
        if tr.inflow_transaction_id is not None:  # ty: ignore[unresolved-attribute]
            transfer_members.add(tr.inflow_transaction_id)  # ty: ignore[unresolved-attribute]
    untouchable = split_parents | transfer_members
    for txn in matched:
        if txn.id in untouchable:
            out.skipped.append(txn)
        elif txn.reviewed_at is None:
            out.unreviewed.append(txn)
        else:
            out.reviewed.append(txn)
    return out


async def clear_proposals(txns: "list[Transaction]") -> None:
    """Vacate the pending proposals on matching unreviewed transactions so
    the next sweep re-proposes under the now-active rule — the rule still
    never writes user data; the Inbox refresh IS the apply. The caller
    defers classify_ledger after commit."""
    if not txns:
        return
    ids = [t.id for t in txns]
    proposals = await Proposal.where(lambda p, tids=ids: p.transaction_id.in_(tids)).all()
    if not proposals:
        return
    proposal_ids = [p.id for p in proposals]
    await ProposalTag.where(lambda pt, pids=proposal_ids: pt.proposal_id.in_(pids)).delete()
    await Proposal.where(lambda p, pids=proposal_ids: p.id.in_(pids)).delete()


async def apply_to_reviewed(
    ledger: Ledger, rule: Rule, txns: "list[Transaction]"
) -> "tuple[uuid.UUID, int]":
    """The full tier: a user-consented bulk edit over reviewed matches
    (CONTEXT.md: Retro-apply). Each transaction is recategorized in place —
    reviewed state untouched, nothing returns to the Inbox — and logged as
    its own DECISION entry sharing one batch id (the Learning tab collapses
    on it). The user is the actor: this is their bulk edit executed via the
    rule, never the pipeline overreaching. No undo — the entries snapshot
    prior state; recovery is editing again. Caller wraps in a transaction.
    """
    from pinch_backend.tags import apply_tag_set, dedupe_tag_names

    batch_id = uuid.uuid7()
    rule_category_id: uuid.UUID | None = rule.action_category_id  # ty: ignore[unresolved-attribute]
    category_name: str | None = None
    if rule_category_id is not None:
        row = await Category.get(rule_category_id)
        category_name = row.name
    applied = 0
    for txn in txns:
        txn_id = txn.id
        existing_ids = [
            tt.tag_id  # ty: ignore[unresolved-attribute]
            for tt in await TransactionTag.where(
                lambda tt, tid=txn_id: tt.transaction_id == tid
            ).all()
        ]
        existing_names = (
            [t.name for t in await Tag.where(lambda t, ids=existing_ids: t.id.in_(ids)).all()]
            if existing_ids
            else []
        )
        final_tags = dedupe_tag_names(existing_names + list(rule.action_add_tags))
        final_category_id = (
            rule_category_id if rule_category_id is not None else txn.category_id  # ty: ignore[unresolved-attribute]
        )
        final_display = rule.action_rename_to or txn.display_name
        await CorrectionLogEntry.create(
            ledger=ledger,
            transaction_id=txn.id,
            kind=CorrectionKind.DECISION,
            actor=CorrectionActor.USER,
            batch_id=batch_id,
            accepted_untouched=False,
            input_description_raw=txn.description_raw,
            input_payee=txn.description_normalized,
            input_amount_minor=txn.amount_minor,
            input_currency=txn.currency,
            input_date=txn.date,
            input_account_id=txn.account_id,  # ty: ignore[unresolved-attribute]
            proposal_provenance=ProposalProvenance.NONE,
            decision_category_id=final_category_id,
            decision_category_name=(
                category_name if final_category_id == rule_category_id else None
            ),
            decision_tags=final_tags,
            decision_display_name=final_display,
        )
        await apply_tag_set(ledger, txn, final_tags)
        txn.category_id = final_category_id  # ty: ignore[unresolved-attribute]
        if final_display is not None:
            txn.display_name = final_display
        await txn.save()
        applied += 1
    return batch_id, applied
