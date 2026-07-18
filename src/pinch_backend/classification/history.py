"""Exact payee history (PRD M5 D12, extended M6 CP4): the most recently
DECIDED reviewed transaction with the same payee that is categorized
(propose its category) OR in an UNTRACKED transfer (propose marking a
transfer), ledger-wide. Decision recency (reviewed_at), not transaction
date — a fresh correction of an old transaction immediately becomes the
payee's signal (M5 CP3 brainstorm). Source of truth is live transactions,
never the log: undo-safe for free — a dissolved transfer stops signalling
the moment it dissolves. Reviewed-but-uncategorized is not a signal (the
user shrugged, they didn't decide), and LINKED transfers are deliberately
not signals either: the counterpart exists, so proposing "untracked" for
the payee would be wrong (that pairing is M7's detector).

A returned match with ``category_id`` None is, by construction of the
predicate, the untracked-transfer signal.
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
                & (
                    (t.category_id != None)  # noqa: E711
                    # In an untracked transfer: this side present, other absent.
                    | (t.transfer_out.exists(lambda tr: tr.inflow_transaction_id == None))  # noqa: E711
                    | (t.transfer_in.exists(lambda tr: tr.outflow_transaction_id == None))  # noqa: E711
                )
            )
        )
        .order_by(lambda t: t.reviewed_at, "desc")
        .order_by(lambda t: t.id, "desc")
        .first()
    )
