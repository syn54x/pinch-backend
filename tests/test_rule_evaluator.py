"""matches() semantics matrix (M5 CP2, #20): pure Python, in-memory rows."""

import uuid
from datetime import date

import pytest

from pinch_backend.imports.fingerprint import normalize_description
from pinch_backend.models import Account, AccountKind, Ledger, Transaction, provision_user
from pinch_backend.rules.evaluator import matches, scan_matches
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


def test_zero_amount_matches_neither_out_nor_in() -> None:
    # A $0 transaction is neither money out nor money in; only `either`
    # may consider it (and then magnitude 0 can never equal a gt=0 value).
    out = _spec({"amount": {"op": "equals", "value": 950, "direction": "out", "currency": "USD"}})
    into = _spec({"amount": {"op": "equals", "value": 950, "direction": "in", "currency": "USD"}})
    assert matches(out, _txn(amount_minor=0)) is False
    assert matches(into, _txn(amount_minor=0)) is False


def test_day_of_month_equals_and_upper_bound() -> None:
    spec_eq = _spec({"day_of_month": {"op": "equals", "value": 30}})
    assert matches(spec_eq, _txn(date=date(2026, 1, 30))) is True
    assert matches(spec_eq, _txn(date=date(2026, 1, 29))) is False
    spec_between = _spec({"day_of_month": {"op": "between", "lo": 28, "hi": 31}})
    assert matches(spec_between, _txn(date=date(2026, 1, 31))) is True  # hi inclusive


def test_amount_without_currency_is_a_loud_error() -> None:
    spec = _spec({"amount": {"op": "equals", "value": 950, "direction": "out"}})
    with pytest.raises(ValueError, match="currency"):
        matches(spec, _txn())


# --- SQL narrowing + scan (DB-backed) ----------------------------------------


async def _seed(db, rows) -> Ledger:
    """rows: list of (description_raw, amount_minor, date) on one account."""
    await provision_user(email="taylor@example.com", display_name="Taylor")
    ledger = (await Ledger.all())[0]
    account = await Account.create(
        ledger=ledger, kind=AccountKind.DEPOSITORY, label="Chk", currency="USD"
    )
    for raw, amount, d in rows:
        await Transaction.create(
            ledger=ledger,
            account=account,
            date=d,
            amount_minor=amount,
            currency="USD",
            description_raw=raw,
            description_normalized=normalize_description(raw),
            fingerprint=uuid.uuid7().hex,
        )
    return ledger


async def test_scan_finds_matches_and_reports_truncation(db) -> None:
    ledger = await _seed(
        db,
        [(f"COSTCO WHSE #{i}", -100 - i, date(2026, 1, 10)) for i in range(3)]
        + [("SHELL OIL", -4000, date(2026, 1, 11))],
    )
    spec = _spec({"payee": {"op": "contains", "value": "costco"}})
    found, truncated = await scan_matches(spec, ledger.id, cap=50)
    assert len(found) == 3 and truncated is False
    found2, truncated2 = await scan_matches(spec, ledger.id, cap=2)
    assert len(found2) == 2 and truncated2 is True


async def test_literal_like_metacharacter_matches_exactly(db) -> None:
    # "100% JUICE" contains a LIKE metacharacter: narrowing must skip, and
    # the evaluator must still match it — and NOT match "100 JUICE".
    ledger = await _seed(
        db,
        [("100% JUICE CO", -500, date(2026, 1, 5)), ("100 JUICE CO", -600, date(2026, 1, 6))],
    )
    spec = _spec({"payee": {"op": "contains", "value": "100% juice"}})
    found, _ = await scan_matches(spec, ledger.id, cap=50)
    assert [t.description_raw for t in found] == ["100% JUICE CO"]


async def test_amount_narrowing_direction_either_finds_both_signs(db) -> None:
    ledger = await _seed(
        db,
        [
            ("REFUND", 950, date(2026, 1, 5)),
            ("CHARGE", -950, date(2026, 1, 6)),
            ("OTHER", -100, date(2026, 1, 7)),
        ],
    )
    spec = _spec(
        {"amount": {"op": "equals", "value": 950, "direction": "either", "currency": "USD"}}
    )
    found, _ = await scan_matches(spec, ledger.id, cap=50)
    assert {t.description_raw for t in found} == {"REFUND", "CHARGE"}


async def test_narrow_rejects_currencyless_amount_like_matches_does(db) -> None:
    spec = _spec({"amount": {"op": "equals", "value": 950, "direction": "out"}})
    with pytest.raises(ValueError, match="currency"):
        await scan_matches(spec, uuid.uuid7(), cap=50)
