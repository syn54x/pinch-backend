"""Row parsing: CSV text + a confirmed MappingSpec → parsed values or
per-row errors (PRD M4 #15).

Money discipline (I-1, CONTEXT.md): an amount that cannot resolve exactly
to integer minor units is invalid — rounding never invents cents. Errors
carry the offending value; this is the user's own data shown back in the
preview, not a reflected request.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Aliased because fields literally named ``date`` (the locked
    # convention) shadow the type in PEP 649's annotation scope.
    from datetime import date as CalendarDate

    from pinch_backend.imports.spec import MappingSpec

CURRENCY_EXPONENT_EXCEPTIONS = {
    # ISO 4217 minor-unit exponents where they differ from the usual 2.
    "BHD": 3, "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "IQD": 3, "ISK": 0,
    "JOD": 3, "JPY": 0, "KMF": 0, "KRW": 0, "KWD": 3, "LYD": 3, "OMR": 3,
    "PYG": 0, "RWF": 0, "TND": 3, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0,
    "XOF": 0, "XPF": 0,
}  # fmt: skip


def currency_exponent(currency: str) -> int:
    return CURRENCY_EXPONENT_EXCEPTIONS.get(currency, 2)


_AMOUNT_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_CURRENCY_SYMBOLS = "$€£¥₩₹"


def parse_amount_minor(text: str, *, exponent: int) -> int:
    """Parse one money value into integer minor units, exactly.

    Accepts an optional leading minus or accounting parentheses, an optional
    currency symbol, and strict thousands grouping ("1,234.56"). A comma
    that isn't strict grouping fails — "12,34" is never silently read as
    1234 or as a decimal comma. Raises ValueError on anything inexact.
    """
    s = text.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    if s[:1] in _CURRENCY_SYMBOLS:
        s = s[1:].strip()
    if not _AMOUNT_RE.match(s):
        raise ValueError(f"unparseable amount: {text!r}")
    try:
        value = Decimal(s.replace(",", ""))
    except InvalidOperation:  # pragma: no cover — the regex should preclude this
        raise ValueError(f"unparseable amount: {text!r}") from None
    scaled = value * (10**exponent)
    if scaled != int(scaled):
        raise ValueError(f"amount {text!r} does not resolve exactly to minor units")
    minor = int(scaled)
    return -minor if negative else minor


@dataclass
class ParsedRow:
    raw_cells: list[str]
    date: "CalendarDate | None" = None
    amount_minor: int | None = None
    description_raw: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _parse_date(cell: str, date_format: str) -> "CalendarDate":
    return datetime.strptime(cell.strip(), date_format).date()


def _parse_amount(cells: list[str], spec: "MappingSpec", exponent: int) -> int:
    if spec.amount_column is not None:
        amount = parse_amount_minor(cells[spec.amount_column], exponent=exponent)
        return -amount if spec.sign == "positive_out" else amount
    debit = cells[spec.debit_column].strip()  # ty: ignore[invalid-argument-type]
    credit = cells[spec.credit_column].strip()  # ty: ignore[invalid-argument-type]
    if debit and credit:
        raise ValueError("both debit and credit are set")
    if debit:
        return -parse_amount_minor(debit, exponent=exponent)
    if credit:
        return parse_amount_minor(credit, exponent=exponent)
    raise ValueError("neither debit nor credit is set")


def parse_rows(text: str, spec: "MappingSpec", *, exponent: int) -> list[ParsedRow]:
    """Parse every data record; blank records are skipped, a header record
    is skipped when the spec says one exists. Never raises on bad data —
    that's what per-row errors are for."""
    records = [cells for cells in csv.reader(io.StringIO(text), delimiter=spec.delimiter) if cells]
    if spec.has_header:
        records = records[1:]

    referenced = [spec.date_column, *spec.description_columns]
    for column in (spec.amount_column, spec.debit_column, spec.credit_column):
        if column is not None:
            referenced.append(column)
    needed = max(referenced) + 1

    rows: list[ParsedRow] = []
    for cells in records:
        row = ParsedRow(raw_cells=cells)
        if len(cells) < needed:
            row.errors.append(f"expected at least {needed} columns, got {len(cells)}")
            rows.append(row)
            continue
        try:
            row.date = _parse_date(cells[spec.date_column], spec.date_format)
        except ValueError:
            row.errors.append(f"unparseable date: {cells[spec.date_column]!r}")
        try:
            row.amount_minor = _parse_amount(cells, spec, exponent)
        except ValueError as error:
            row.errors.append(str(error))
        row.description_raw = " ".join(
            cells[column].strip() for column in spec.description_columns
        ).strip()
        rows.append(row)
    return rows
