"""ConditionSpec validation matrix (M5 CP2, #20): pure pydantic, no DB."""

import pytest
from pydantic import ValidationError

from pinch_backend.rules.spec import ConditionSpec


def test_minimal_payee_condition_validates() -> None:
    spec = ConditionSpec.model_validate({"payee": {"op": "contains", "value": "COSTCO"}})
    assert spec.version == 1
    assert spec.payee.op == "contains"


def test_empty_condition_set_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one condition"):
        ConditionSpec.model_validate({})


def test_unknown_version_is_rejected_loudly() -> None:
    with pytest.raises(ValidationError):
        ConditionSpec.model_validate({"version": 2, "payee": {"op": "equals", "value": "x"}})


def test_amount_equals_requires_value_not_range() -> None:
    with pytest.raises(ValidationError):
        ConditionSpec.model_validate(
            {"amount": {"op": "equals", "lo": 1, "hi": 2, "direction": "out", "currency": "USD"}}
        )
    spec = ConditionSpec.model_validate(
        {"amount": {"op": "equals", "value": 999, "direction": "out", "currency": "USD"}}
    )
    assert spec.amount.value == 999


def test_amount_between_requires_ordered_range() -> None:
    with pytest.raises(ValidationError):
        ConditionSpec.model_validate(
            {
                "amount": {
                    "op": "between",
                    "lo": 200,
                    "hi": 100,
                    "direction": "out",
                    "currency": "USD",
                }
            }
        )


def test_day_of_month_bounds() -> None:
    with pytest.raises(ValidationError):
        ConditionSpec.model_validate({"day_of_month": {"op": "equals", "value": 32}})
    spec = ConditionSpec.model_validate({"day_of_month": {"op": "between", "lo": 28, "hi": 31}})
    assert (spec.day_of_month.lo, spec.day_of_month.hi) == (28, 31)


def test_currency_is_optional_on_the_wire() -> None:
    # The API fills it from the user's primary before storage; the spec
    # itself permits omission so the wire shape stays ergonomic.
    spec = ConditionSpec.model_validate(
        {"amount": {"op": "equals", "value": 5, "direction": "either"}}
    )
    assert spec.amount.currency is None


def test_whitespace_only_payee_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="blank"):
        ConditionSpec.model_validate({"payee": {"op": "contains", "value": " "}})


def test_unknown_condition_key_is_rejected_loudly() -> None:
    with pytest.raises(ValidationError):
        ConditionSpec.model_validate(
            {"day_of_week": {"op": "equals", "value": 1}, "payee": {"op": "equals", "value": "x"}}
        )


def test_unknown_nested_condition_key_is_rejected_loudly() -> None:
    with pytest.raises(ValidationError):
        ConditionSpec.model_validate(
            {"payee": {"op": "equals", "value": "x", "case_sensitive": True}}
        )


def test_all_three_types_compose() -> None:
    spec = ConditionSpec.model_validate(
        {
            "payee": {"op": "equals", "value": "rent llc"},
            "amount": {
                "op": "between",
                "lo": 100000,
                "hi": 200000,
                "direction": "out",
                "currency": "USD",
            },
            "day_of_month": {"op": "between", "lo": 28, "hi": 31},
        }
    )
    assert spec.payee and spec.amount and spec.day_of_month
