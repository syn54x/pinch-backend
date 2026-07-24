"""The mapping agent (PRD M9 CP5): layered behind the M4 inferrer seam,
never replacing it. The deterministic heuristic runs first, exactly as
today — normal files never cost a token, keyless instances keep today's
behavior verbatim. Only when it abstains does Penny read a bounded sample
and propose a MappingSpec, validated against the actual columns; nonsense
draws ModelRetry, persistent nonsense degrades to no suggestion — manual
mapping, today's floor. The suggestion flows into the same
user-confirms-or-corrects step as every suggested mapping, and a confirmed
mapping still becomes an import profile: any inferrer runs at most once
per file shape.
"""

import csv
import io
from datetime import datetime

from pydantic_ai import Agent, ModelRetry, RunContext

from pinch_backend.imports.spec import MappingSpec
from pinch_backend.observability import get_logger
from pinch_backend.penny.availability import mapping_availability
from pinch_backend.settings import settings

log = get_logger(__name__)

SAMPLE_LINES = 20
"""The bounded sample (PRD M9): first ~20 lines, never the whole file."""

MAPPING_INSTRUCTIONS = """\
You map one bank-export CSV sample onto a column specification.

The spec you produce:
- delimiter: the single field-separator character.
- has_header: whether the first line is column names.
- date_column (0-based) and date_format: a Python strptime format that
  parses the sampled date values ("%Y-%m-%d", "%b-%d-%y", ...).
- Either amount_column (one signed amount column) OR the
  debit_column/credit_column pair (money out / money in) — never both.
- sign: for a single amount column, "negative_out" when negatives are
  money out (including parenthesized negatives), "positive_out" when the
  file lists charges as positives.
- description_columns: the columns that together describe the merchant or
  purpose, in reading order.

Column indices refer to the delimiter-split fields of the sample you were
given. Do not invent columns that aren't there.
"""


# ty can't thread output_type through Agent's constructor overloads; the
# annotation states what output_type=MappingSpec already enforces.
mapping_agent: Agent[str, MappingSpec] = Agent(  # ty: ignore[invalid-assignment]
    deps_type=str,
    output_type=MappingSpec,
    instructions=MAPPING_INSTRUCTIONS,
)


@mapping_agent.output_validator
def _spec_fits_the_sample(ctx: RunContext[str], spec: MappingSpec) -> MappingSpec:
    """Validated against the actual columns (PRD M9): a spec that doesn't
    survive contact with the sample is sent back with the reason."""
    reader = csv.reader(io.StringIO(ctx.deps), delimiter=spec.delimiter)
    rows = [row for row in reader if row]
    data = rows[1:] if spec.has_header else rows
    if not data:
        raise ModelRetry("With that delimiter and header choice the sample has no data rows.")
    width = max(len(row) for row in data)

    for name in ("date_column", "amount_column", "debit_column", "credit_column"):
        column = getattr(spec, name)
        if column is not None and column >= width:
            raise ModelRetry(f"{name}={column} is outside the sample's {width} columns (0-based).")
    for column in spec.description_columns:
        if column >= width:
            raise ModelRetry(
                f"description_columns contains {column}, outside the sample's "
                f"{width} columns (0-based)."
            )

    date_values = [
        row[spec.date_column].strip()
        for row in data
        if len(row) > spec.date_column and row[spec.date_column].strip()
    ]

    def parses(value: str) -> bool:
        try:
            datetime.strptime(value, spec.date_format)
        except ValueError:
            return False
        return True

    if not date_values or sum(1 for value in date_values if parses(value)) * 2 <= len(date_values):
        raise ModelRetry(
            f"date_format {spec.date_format!r} does not parse the sampled values in "
            f"column {spec.date_column} (for example {date_values[:2]})."
        )
    return spec


def bounded_sample(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:SAMPLE_LINES])


class PennyInferrer:
    """The seam implementation: heuristic first, agent only on abstention,
    every failure shape the same honest answer — no suggestion."""

    def __init__(self) -> None:
        # Runtime import: inference.py imports this module at its tail to
        # perform the seam swap, so the heuristic is resolved lazily.
        from pinch_backend.imports.inference import HeuristicInferrer

        self._heuristic = HeuristicInferrer()

    async def suggest(self, text: str) -> MappingSpec | None:
        spec = await self._heuristic.suggest(text)
        if spec is not None:
            return spec
        if not mapping_availability().available:
            return None
        sample = bounded_sample(text)
        try:
            result = await mapping_agent.run(
                f"Map this bank-export sample:\n\n{sample}",
                deps=sample,
                model=settings.ai_mapping_model,
            )
        except Exception as error:
            log.warning("penny.mapping.abstained", error=str(error))
            return None
        return result.output
