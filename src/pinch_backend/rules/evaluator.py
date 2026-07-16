"""The single source of rule-condition semantics (PRD M5, D14).

`matches()` is correctness; SQL narrowing (Task 4's `narrow`) is an
optimization layered in front of it. The pipeline (CP3), the preview, and
retroactive application all call this module — adding a condition type
later touches spec.py and this file, nothing else.
"""

from typing import TYPE_CHECKING

from pinch_backend.api.pagination import paginate
from pinch_backend.imports.fingerprint import normalize_description
from pinch_backend.models import Transaction

if TYPE_CHECKING:
    import uuid

    from pinch_backend.rules.spec import ConditionSpec


def matches(spec: "ConditionSpec", txn: "Transaction") -> bool:
    """Does ``txn`` satisfy every clause of ``spec``? Pure — no I/O."""
    if spec.payee is not None:
        needle = normalize_description(spec.payee.value)
        hay = txn.description_normalized
        if spec.payee.op == "equals":
            if hay != needle:
                return False
        elif needle not in hay:
            return False

    if spec.amount is not None:
        clause = spec.amount
        if clause.currency is None:
            # Stored conditions are always explicit (the API fills the user's
            # primary); evaluating a currencyless clause is a caller bug.
            raise ValueError("amount condition has no currency; specs must be storage-complete")
        if txn.currency != clause.currency:
            return False
        if clause.direction == "out" and txn.amount_minor >= 0:
            return False
        if clause.direction == "in" and txn.amount_minor <= 0:
            return False
        magnitude = abs(txn.amount_minor)
        if clause.op == "equals":
            if magnitude != clause.value:
                return False
        elif not (clause.lo <= magnitude <= clause.hi):  # ty: ignore[unsupported-operator]
            return False

    if spec.day_of_month is not None:
        clause = spec.day_of_month
        day = txn.date.day
        if clause.op == "equals":
            if day != clause.value:
                return False
        elif not (clause.lo <= day <= clause.hi):  # ty: ignore[unsupported-operator]
            return False

    return True


SCAN_BATCH = 500
"""Keyset batch size for narrowed scans; correctness is matches()'s."""


def narrow(spec: "ConditionSpec", query):
    """Best-effort SQL pre-filter: never wrong, sometimes absent.

    - payee equals: always narrows with `==` — LIKE metacharacters (and the
      backslash escape char below) are inert in equality, so there's nothing
      to skip.
    - payee contains: LIKE — but only when the normalized value carries no
      LIKE metacharacter. Postgres's default LIKE escape char is backslash,
      and ferro's like() has no ESCAPE support, so portable escaping is
      impossible; a value containing `%`, `_`, or `\\` simply skips
      narrowing and lets matches() decide.
    - amount: currency equality + sign-aware magnitude range (OR for either).
    - day_of_month: no SQL (ferro has no date-part extraction — by design).
    """
    if spec.payee is not None:
        needle = normalize_description(spec.payee.value)
        if spec.payee.op == "equals":
            query = query.where(lambda t, n=needle: t.description_normalized == n)
        elif "%" not in needle and "_" not in needle and "\\" not in needle:
            pattern = f"%{needle}%"
            query = query.where(lambda t, p=pattern: t.description_normalized.like(p))
    if spec.amount is not None:
        clause = spec.amount
        if clause.currency is None:
            raise ValueError("amount condition has no currency; specs must be storage-complete")
        query = query.where(lambda t, c=clause.currency: t.currency == c)
        lo = clause.value if clause.op == "equals" else clause.lo
        hi = clause.value if clause.op == "equals" else clause.hi
        neg_lo, neg_hi = -lo, -hi  # ty: ignore[unsupported-operator]
        if clause.direction == "out":
            query = query.where(
                lambda t, lo=neg_hi, hi=neg_lo: (t.amount_minor >= lo) & (t.amount_minor <= hi)
            )
        elif clause.direction == "in":
            query = query.where(
                lambda t, lo=lo, hi=hi: (t.amount_minor >= lo) & (t.amount_minor <= hi)
            )
        else:
            query = query.where(
                lambda t, lo=lo, hi=hi, nlo=neg_hi, nhi=neg_lo: (
                    ((t.amount_minor >= lo) & (t.amount_minor <= hi))
                    | ((t.amount_minor >= nlo) & (t.amount_minor <= nhi))
                )
            )
    return query


async def scan_matches(
    spec: "ConditionSpec", ledger_id: "uuid.UUID", *, cap: int
) -> tuple[list[Transaction], bool]:
    """Up to ``cap`` matching transactions (id order) + a truncation flag.

    Narrowed SQL keyset batches, finished in Python by matches(). Worst case
    scans the ledger (a CP1-volumes seam, same class as the tag filter);
    the preview is a capped sample, never a cursor walk (D14).
    """
    base = Transaction.where(lambda t, lid=ledger_id: t.ledger_id == lid)
    base = narrow(spec, base)
    matched: list[Transaction] = []
    cursor: str | None = None
    while True:
        rows, cursor = await paginate(base, cursor=cursor, limit=SCAN_BATCH)
        for txn in rows:
            if matches(spec, txn):
                if len(matched) == cap:
                    return matched, True
                matched.append(txn)
        if cursor is None:
            return matched, False
