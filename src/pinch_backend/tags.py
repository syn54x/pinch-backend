"""Tag resolution shared by the tags API and the transaction PATCH (M5 CP1).

Tags are free-form and created implicitly on first use; ``name_fold`` (the
casefolded name) is the identity, so "Vacation" and "vacation" never fork.
"""

from typing import TYPE_CHECKING

from pinch_backend.models import Ledger, Tag, TransactionTag

if TYPE_CHECKING:
    from pinch_backend.models import Transaction


async def resolve_tags(ledger: Ledger, names: list[str]) -> list[Tag]:
    """Return the Tag rows for ``names`` in the ledger, creating any that are
    new. Deduped by casefold, order preserved by first appearance."""
    result: list[Tag] = []
    seen: set[str] = set()
    for name in names:
        fold = name.strip().casefold()
        if not fold or fold in seen:
            continue
        seen.add(fold)
        ledger_id = ledger.id
        tag = await Tag.where(
            lambda t, ledger_id=ledger_id, fold=fold: (
                (t.ledger_id == ledger_id) & (t.name_fold == fold)
            )
        ).first()
        if tag is None:
            tag = await Tag.create(ledger=ledger, name=name.strip(), name_fold=fold)
        result.append(tag)
    return result


async def apply_tag_set(ledger: Ledger, txn: "Transaction", names: list[str]) -> None:
    """Reconcile ``txn``'s tags to exactly ``names``: implicit-create new
    ones, detach removed ones. Shared by the transaction PATCH and the
    consume-proposal operation (M5 CP3) — one reconciliation semantics."""
    wanted = await resolve_tags(ledger, names)
    wanted_ids = {t.id for t in wanted}
    txn_id = txn.id
    existing = await TransactionTag.where(lambda tt, tid=txn_id: tt.transaction_id == tid).all()
    existing_ids = {tt.tag_id for tt in existing}  # ty: ignore[unresolved-attribute]
    for tt in existing:
        if tt.tag_id not in wanted_ids:  # ty: ignore[unresolved-attribute]
            await tt.delete()
    for tg in wanted:
        if tg.id not in existing_ids:
            await TransactionTag.create(ledger=ledger, transaction=txn, tag=tg)


def dedupe_tag_names(names: list[str]) -> list[str]:
    """Trim + casefold-dedupe, first casing wins — the same fold rule as
    resolve_tags. Review payloads normalize BEFORE consume (M5 CP4) so
    decision_tags logs exactly the applied set, never a raw superset."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        fold = name.strip().casefold()
        if fold and fold not in seen:
            seen.add(fold)
            out.append(name.strip())
    return out
