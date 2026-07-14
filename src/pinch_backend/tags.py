"""Tag resolution shared by the tags API and the transaction PATCH (M5 CP1).

Tags are free-form and created implicitly on first use; ``name_fold`` (the
casefolded name) is the identity, so "Vacation" and "vacation" never fork.
"""

from pinch_backend.models import Ledger, Tag


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
