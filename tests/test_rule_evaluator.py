"""matches() semantics matrix (M5 CP2, #20): pure Python, in-memory rows."""

import uuid
from datetime import date

import pytest

from pinch_backend.models import Transaction
from pinch_backend.rules.evaluator import matches
from pinch_backend.rules.spec import ConditionSpec


def _txn(**over) -> Transaction:
    """An unsaved Transaction — matches() never touches the database."""
    base = dict(
        ledger_id=uuid.uuid7(),
        account_id=uuid.uuid7(),
        date=date(2026, 2, 28),
        amount_minor=-950,
        currency="USD",
        description_raw="COSTCO WHSE #0482",
        description_normalized="costco whse #0482",
        fingerprint="f" * 64,
    )
    base.update(over)
    return Transaction(**base)  # ty: ignore[missing-argument]


def _spec(payload: dict) -> ConditionSpec:
    return ConditionSpec.model_validate(payload)


def test_payee_equals_is_case_and_whitespace_insensitive() -> None:
    spec = _spec({"payee": {"op": "equals", "value": "  Costco   WHSE  #0482 "}})
    assert matches(spec, _txn()) is True


def test_payee_contains_substring_on_normalized() -> None:
    assert matches(_spec({"payee": {"op": "contains", "value": "COSTCO"}}), _txn()) is True
    assert matches(_spec({"payee": {"op": "contains", "value": "SHELL"}}), _txn()) is False


def test_amount_equals_magnitude_and_direction() -> None:
    spec = _spec({"amount": {"op": "equals", "value": 950, "direction": "out", "currency": "USD"}})
    assert matches(spec, _txn(amount_minor=-950)) is True
    assert matches(spec, _txn(amount_minor=950)) is False  # money in, direction out
    spec_in = _spec(
        {"amount": {"op": "equals", "value": 950, "direction": "in", "currency": "USD"}}
    )
    assert matches(spec_in, _txn(amount_minor=950)) is True
    spec_either = _spec(
        {"amount": {"op": "equals", "value": 950, "direction": "either", "currency": "USD"}}
    )
    assert matches(spec_either, _txn(amount_minor=950)) is True
    assert matches(spec_either, _txn(amount_minor=-950)) is True


def test_amount_currency_isolation() -> None:
    spec = _spec({"amount": {"op": "equals", "value": 950, "direction": "out", "currency": "EUR"}})
    assert matches(spec, _txn(amount_minor=-950, currency="USD")) is False


def test_amount_between_bounds_inclusive() -> None:
    spec = _spec(
        {"amount": {"op": "between", "lo": 900, "hi": 1000, "direction": "out", "currency": "USD"}}
    )
    assert matches(spec, _txn(amount_minor=-900)) is True
    assert matches(spec, _txn(amount_minor=-1000)) is True
    assert matches(spec, _txn(amount_minor=-899)) is False


def test_day_between_28_31_matches_feb_28() -> None:
    spec = _spec({"day_of_month": {"op": "between", "lo": 28, "hi": 31}})
    assert matches(spec, _txn(date=date(2026, 2, 28))) is True
    assert matches(spec, _txn(date=date(2026, 2, 27))) is False


def test_conditions_and_compose() -> None:
    spec = _spec(
        {
            "payee": {"op": "contains", "value": "costco"},
            "amount": {"op": "equals", "value": 950, "direction": "out", "currency": "USD"},
        }
    )
    assert matches(spec, _txn()) is True
    assert matches(spec, _txn(description_normalized="shell oil")) is False
    assert matches(spec, _txn(amount_minor=-951)) is False


def test_amount_without_currency_is_a_loud_error() -> None:
    spec = _spec({"amount": {"op": "equals", "value": 950, "direction": "out"}})
    with pytest.raises(ValueError, match="currency"):
        matches(spec, _txn())
