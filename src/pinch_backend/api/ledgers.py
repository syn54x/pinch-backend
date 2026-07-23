"""/api/v1/ledgers — ledger-level derived numbers (PRD M8 #45, CP5 #51).

One poll target for onboarding step 3 ("Penny is reading your history":
processed/total, recurring found — pinch-frontend#15's deferral, honored)
and the Dashboard's to-review trust split. Counts are live because
classification and detection run in the same deferred job chain the
initial sync uses.
"""

from datetime import datetime

from litestar import Router, get
from litestar.di import NamedDependency
from pydantic import BaseModel

from pinch_backend.models import (
    Connection,
    Ledger,
    Proposal,
    ProposalProvenance,
    RecurringSeries,
    RecurringStatus,
    Transaction,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)


class LedgerStatsOut(BaseModel):
    """``classified`` counts transactions the pipeline has answered for —
    reviewed, or carrying a proposal (an empty proposal counts: the
    pipeline ran and abstained honestly). ``recurring_found`` is active
    series; null only on an instance shipped without the recurring engine."""

    transactions_total: int
    classified: int
    unreviewed: int
    unreviewed_by_provenance: dict[str, int]
    recurring_found: int | None
    last_synced_at: datetime | None


@get("/current/stats")
async def ledger_stats(current_ledger: NamedDependency[Ledger]) -> LedgerStatsOut:
    ledger_id = current_ledger.id
    total = await Transaction.where(lambda t: t.ledger_id == ledger_id).count()
    unreviewed = await Transaction.where(
        lambda t: (t.ledger_id == ledger_id) & (t.reviewed_at == None)  # noqa: E711
    ).count()
    reviewed = total - unreviewed
    proposed = await Transaction.where(
        lambda t: (
            (t.ledger_id == ledger_id)
            & (t.reviewed_at == None)  # noqa: E711
            & t.proposals.exists()
        )
    ).count()

    provenance_rows = (
        await Proposal.select(lambda p: {"provenance": p.provenance, "n": p.id.count()})
        .where(
            lambda p: (p.ledger_id == ledger_id) & (p.transaction.reviewed_at == None)  # noqa: E711
        )
        .all()
    )
    split = {provenance.value: 0 for provenance in ProposalProvenance}
    for row in provenance_rows:
        split[row.provenance.value] = row.n  # ty: ignore[unresolved-attribute]

    recurring_found = await RecurringSeries.where(
        lambda s: (s.ledger_id == ledger_id) & (s.status == RecurringStatus.ACTIVE)
    ).count()

    connections = await Connection.where(lambda c: c.ledger_id == ledger_id).all()
    synced_times = [c.last_synced_at for c in connections if c.last_synced_at is not None]

    return LedgerStatsOut(
        transactions_total=total,
        classified=reviewed + proposed,
        unreviewed=unreviewed,
        unreviewed_by_provenance=split,
        recurring_found=recurring_found,
        last_synced_at=max(synced_times) if synced_times else None,
    )


ledgers_router = Router(path="/api/v1/ledgers", route_handlers=[ledger_stats])
