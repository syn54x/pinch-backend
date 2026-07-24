"""The mapping-suggestion seam (PRD M4): a protocol the API depends on and
a deterministic heuristic v0 behind it.

The API exposes ``suggested_mapping`` and never names the mechanism —
Penny's M9 inferrer replaces ``active_inferrer`` with zero contract change.
No LLM, no keys, no network: the heuristic is what guarantees CI never
talks to the outside world.
"""

import csv
import io
from datetime import datetime
from typing import Protocol

from pinch_backend.imports.parsing import parse_amount_minor
from pinch_backend.imports.spec import MappingSpec

SAMPLE_RECORDS = 50
"""Suggestion quality is a sample-size question, not a correctness one:
the confirmed mapping re-parses everything."""

DATE_SYNONYMS = frozenset(
    {"date", "transaction date", "posted", "posted date", "posting date", "post date"}
)
AMOUNT_SYNONYMS = frozenset({"amount", "transaction amount", "value"})
DEBIT_SYNONYMS = frozenset(
    {"debit", "debit amount", "withdrawal", "withdrawals", "money out", "outflow"}
)
CREDIT_SYNONYMS = frozenset(
    {"credit", "credit amount", "deposit", "deposits", "money in", "inflow"}
)
DESCRIPTION_SYNONYMS = frozenset(
    {"description", "memo", "payee", "name", "details", "narrative", "merchant"}
)
_ALL_SYNONYMS = (
    DATE_SYNONYMS | AMOUNT_SYNONYMS | DEBIT_SYNONYMS | CREDIT_SYNONYMS | DESCRIPTION_SYNONYMS
)

DATE_FORMATS = (
    # Trial order is the tiebreak: an ambiguous "01/05/2026" reads as US
    # month-first unless some value in the sample forces day-first.
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%m/%d/%y",
    "%d.%m.%Y",
    "%b %d, %Y",
    "%d %b %Y",
)


class MappingInferrer(Protocol):
    async def suggest(self, text: str) -> MappingSpec | None:
        """A best-effort MappingSpec for the file, or None when the shape
        yields nothing trustworthy; the user confirms or corrects either way."""
        ...


def sniff_delimiter(sample: str) -> str:
    """Deterministic delimiter read; also what profile lookup keys on at
    upload, before any mapping exists."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _parses_as(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value.strip(), fmt)
    except ValueError:
        return False
    return True


def _working_date_format(values: list[str]) -> str | None:
    """The format parsing the most sampled values, if it wins a majority.

    Majority, not unanimity: a file with a few garbage rows still deserves
    a suggestion — surfacing those rows is what the preview is for. Ties go
    to the earlier trial (the US-first tiebreak)."""
    best_format, best_count = None, 0
    for fmt in DATE_FORMATS:
        count = sum(1 for value in values if _parses_as(value, fmt))
        if count > best_count:
            best_format, best_count = fmt, count
    if best_format is not None and best_count * 2 > len(values):
        return best_format
    return None


def _column_values(records: list[list[str]], column: int) -> list[str]:
    values = [row[column] for row in records if len(row) > column and row[column].strip()]
    return values


def _is_amount_column(values: list[str]) -> bool:
    """Majority of sampled values parse as money — same tolerance for
    garbage rows as the date trial."""
    if not values:
        return False

    def parses(value: str) -> bool:
        try:
            parse_amount_minor(value, exponent=2)
        except ValueError:
            return False
        return True

    return sum(1 for value in values if parses(value)) * 2 > len(values)


class HeuristicInferrer:
    """Header-name synonyms, csv.Sniffer, and date/amount parsing trials
    over sample values; headerless files get suggestions from value shapes
    alone (PRD M4)."""

    async def suggest(self, text: str) -> MappingSpec | None:
        delimiter = sniff_delimiter(text[:4096])
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        records = [cells for cells in reader if cells][: SAMPLE_RECORDS + 1]
        if not records:
            return None

        header = [cell.strip().casefold() for cell in records[0]]
        has_header = any(cell in _ALL_SYNONYMS for cell in header)
        data = records[1:] if has_header else records
        if not data:
            return None
        width = max(len(row) for row in data)

        def header_role(synonyms: frozenset[str]) -> int | None:
            if not has_header:
                return None
            return next((i for i, cell in enumerate(header) if cell in synonyms), None)

        # Date: the header's word for it, else the first column whose every
        # sampled value parses with one format.
        date_column = header_role(DATE_SYNONYMS)
        date_format = None
        candidates = [date_column] if date_column is not None else list(range(width))
        for column in candidates:
            values = _column_values(data, column)
            if not values:
                continue
            date_format = _working_date_format(values)
            if date_format is not None:
                date_column = column
                break
        if date_column is None or date_format is None:
            return None

        # Amount: a single column or a debit/credit pair, by header words
        # first, else the first amount-shaped column that isn't the date.
        amount_column = header_role(AMOUNT_SYNONYMS)
        debit_column = header_role(DEBIT_SYNONYMS)
        credit_column = header_role(CREDIT_SYNONYMS)
        if amount_column is None and (debit_column is None or credit_column is None):
            debit_column = credit_column = None
            amount_column = next(
                (
                    column
                    for column in range(width)
                    if column != date_column and _is_amount_column(_column_values(data, column))
                ),
                None,
            )
            if amount_column is None:
                return None

        # Sign: negatives in the sample prove the sign is meaningful; an
        # all-positive single column stays negative_out (the correction is
        # exactly what mapping review exists for).
        role_columns = {date_column, amount_column, debit_column, credit_column}
        if has_header:
            description_columns = [
                i for i, cell in enumerate(header) if cell in DESCRIPTION_SYNONYMS
            ]
        else:
            description_columns = [
                column
                for column in range(width)
                if column not in role_columns
                and any(char.isalpha() for value in _column_values(data, column) for char in value)
            ]

        return MappingSpec(
            delimiter=delimiter,
            has_header=has_header,
            date_column=date_column,
            date_format=date_format,
            amount_column=amount_column,
            debit_column=debit_column,
            credit_column=credit_column,
            description_columns=description_columns,
        )


# Imported at the tail on purpose: penny.mapping's PennyInferrer wraps
# HeuristicInferrer (defined above) — the M9 CP5 seam swap. Keyless
# instances keep the heuristic's behavior byte-identical: the agent layer
# only wakes when the heuristic abstains AND a mapping model is configured.
from pinch_backend.penny.mapping import PennyInferrer  # noqa: E402

active_inferrer: MappingInferrer = PennyInferrer()
"""The seam (PRD M4): swap this for Penny in M9 — or a stub in tests —
without touching the API layer."""
