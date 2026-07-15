"""The single source of rule-condition semantics (PRD M5, D14).

`matches()` is correctness; SQL narrowing (Task 4's `narrow`) is an
optimization layered in front of it. The pipeline (CP3), the preview, and
retroactive application all call this module — adding a condition type
later touches spec.py and this file, nothing else.
"""

from typing import TYPE_CHECKING

from pinch_backend.imports.fingerprint import normalize_description

if TYPE_CHECKING:
    from pinch_backend.models import Transaction
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
