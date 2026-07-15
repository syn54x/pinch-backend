"""ConditionSpec: the versioned rule-condition vocabulary (PRD M5, D14).

An open, evolving vocabulary stored as JSONB on Rule (the MappingSpec
precedent): at most one condition of each type, AND-composed, at least one
required — OR is "make a second rule". Semantics live exclusively in
pinch_backend.rules.evaluator; this module is shape and validation only.
Unknown versions fail the Literal, loudly (I-1).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _check_equals_between(op: str, value: int | None, lo: int | None, hi: int | None) -> None:
    if op == "equals":
        if value is None or lo is not None or hi is not None:
            raise ValueError("equals takes exactly `value`")
    else:
        if value is not None or lo is None or hi is None:
            raise ValueError("between takes exactly `lo` and `hi`")
        if lo > hi:
            raise ValueError("`lo` must not exceed `hi`")


class PayeeCondition(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    op: Literal["equals", "contains"]
    value: str = Field(min_length=1, max_length=200)
    """Compared against the normalized payee after the same normalization
    (imports.fingerprint.normalize_description) — matching is case- and
    whitespace-insensitive by construction."""


class AmountCondition(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    op: Literal["equals", "between"]
    value: int | None = Field(default=None, gt=0)
    """Magnitude in minor units (positive; direction carries the sign)."""
    lo: int | None = Field(default=None, gt=0)
    hi: int | None = Field(default=None, gt=0)
    direction: Literal["out", "in", "either"]
    """out = money out (amount_minor < 0), in = money in (> 0)."""
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    """Wire-optional; the API fills the user's primary currency before
    storage, so persisted conditions are always explicit. The condition only
    tests transactions of this currency."""

    @model_validator(mode="after")
    def _shape(self) -> "AmountCondition":
        _check_equals_between(self.op, self.value, self.lo, self.hi)
        return self


class DayOfMonthCondition(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    op: Literal["equals", "between"]
    value: int | None = Field(default=None, ge=1, le=31)
    lo: int | None = Field(default=None, ge=1, le=31)
    hi: int | None = Field(default=None, ge=1, le=31)
    """Literal calendar matching; `between 28-31` is the month-end-drift
    answer (rent "on the 30th" that posts Feb 28). No "the 30th means
    Feb 28" cleverness."""

    @model_validator(mode="after")
    def _shape(self) -> "DayOfMonthCondition":
        _check_equals_between(self.op, self.value, self.lo, self.hi)
        return self


class ConditionSpec(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    version: Literal[1] = 1
    """Schema version. Evolve ADDITIVELY (Literal[1, 2] / discriminated
    union) — never replace the 1, or stored v1 rules fail validation at
    pipeline load."""
    payee: PayeeCondition | None = None
    amount: AmountCondition | None = None
    day_of_month: DayOfMonthCondition | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ConditionSpec":
        if self.payee is None and self.amount is None and self.day_of_month is None:
            raise ValueError("at least one condition is required")
        return self
