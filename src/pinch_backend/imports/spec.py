"""MappingSpec: the profile's payload (PRD M4) — everything needed to parse
a file deterministically.

Column roles are 0-based indexes, not names, so headerless files map with
the same vocabulary. Currency is deliberately absent: it comes from the
account (CONTEXT.md: Money — an amount never travels without its currency,
and an import's currency is its account's).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MappingSpec(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    delimiter: str = Field(min_length=1, max_length=1)
    has_header: bool
    """Whether the first record is column names, excluded from parsing.
    Headerless files never match a profile (CP3): no trustworthy identity."""
    date_column: int = Field(ge=0)
    date_format: str = Field(min_length=1)
    """strptime format; the parsed value is a calendar date, never a
    localized timestamp (the locked Transaction convention)."""
    amount_column: int | None = Field(default=None, ge=0)
    """A single signed amount column — exclusive with the debit/credit pair."""
    debit_column: int | None = Field(default=None, ge=0)
    credit_column: int | None = Field(default=None, ge=0)
    sign: Literal["negative_out", "positive_out"] = "negative_out"
    """How a single amount column encodes direction: ``negative_out`` reads
    the sign as-is (negative = money out, the Transaction convention);
    ``positive_out`` flips it (card exports listing charges as positives).
    Ignored for a debit/credit pair — debit is money out by definition."""
    description_columns: list[int] = Field(default_factory=list)
    """Joined with single spaces into description_raw; may be empty."""

    @model_validator(mode="after")
    def _exactly_one_amount_shape(self) -> "MappingSpec":
        single = self.amount_column is not None
        pair = self.debit_column is not None and self.credit_column is not None
        half_pair = (self.debit_column is None) != (self.credit_column is None)
        if half_pair:
            raise ValueError("debit_column and credit_column must be set together")
        if single == pair:
            raise ValueError("exactly one of amount_column or the debit/credit pair is required")
        return self
