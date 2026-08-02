"""Retro-apply (F4 Enabler B, #67; CONTEXT.md: Retro-apply).

The shared half of the consent contract: split a condition's full match set
into what each tier would touch. Split parents and transfer members keep
their structure — one layer holds categories, and transfer exclusion derives
from membership — so they are *skipped*, and the counts say so honestly
before consent is given.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pinch_backend.models import SplitLine, Transaction, Transfer

if TYPE_CHECKING:
    import uuid


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
    ids: "list[uuid.UUID]" = [t.id for t in matched]
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
