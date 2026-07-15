"""Exact payee history (PRD M5 D12): the most recently DECIDED reviewed,
categorized transaction with the same payee, ledger-wide. Decision recency
(reviewed_at), not transaction date — a fresh correction of an old
transaction immediately becomes the payee's signal (M5 CP3 brainstorm).
Source of truth is live transactions, never the log: undo-safe for free.
Reviewed-but-uncategorized is not a signal (the user shrugged, they didn't
decide).
"""

from typing import TYPE_CHECKING

from pinch_backend.models import Transaction

if TYPE_CHECKING:
    import uuid


async def history_match(ledger_id: "uuid.UUID", payee: str) -> Transaction | None:
    return await (
        Transaction.where(
            lambda t, lid=ledger_id, p=payee: (
                (t.ledger_id == lid)
                & (t.description_normalized == p)
                & (t.reviewed_at != None)  # noqa: E711
                & (t.category_id != None)  # noqa: E711
            )
        )
        .order_by(lambda t: t.reviewed_at, "desc")
        .order_by(lambda t: t.id, "desc")
        .first()
    )
