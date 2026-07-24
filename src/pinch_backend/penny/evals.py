"""The evals harness (PRD M9 CP3): committed datasets, a runner reporting
to Logfire experiments, and the correction-log exporter — the machinery
behind the improvement law: *no prompt or model change merges without
before/after eval numbers.* Offline quality gate, never CI pass/fail.

Scoring is asymmetric by principle: exact category 1.0; an ancestor of the
expected category partial credit; **an abstain scores above a wrong
category** — recurring's "a wrong series is worse than a missing one",
applied to classification.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uuid

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from pinch_backend.penny.categorization import (
    PATH_SEPARATOR,
    categorization_agent,
    format_prompt,
    taxonomy_paths,
)
from pinch_backend.taxonomy import DEFAULT_TAXONOMY

EVALS_ROOT = Path(__file__).parent.parent.parent.parent / "evals"

EXACT = 1.0
ANCESTOR = 0.5
ABSTAINED = 0.25
WRONG = 0.0


def default_taxonomy_paths() -> list[str]:
    """The seed dataset's answer set: the starter taxonomy every new ledger
    gets, as full paths."""
    paths: list[str] = []
    for parent, children in DEFAULT_TAXONOMY:
        paths.append(parent)
        paths.extend(f"{parent}{PATH_SEPARATOR}{child}" for child in children)
    return paths


@dataclass
class CategoryScore(Evaluator[dict, str | None, Any]):
    """The asymmetric score plus the rate flags the headline numbers
    aggregate: accuracy (exact), abstain rate, wrong rate."""

    def evaluate(self, ctx: EvaluatorContext[dict, str | None, Any]) -> dict[str, float | bool]:
        expected, got = ctx.expected_output, ctx.output
        if expected is None:
            score = EXACT if got is None else WRONG
        elif got == expected:
            score = EXACT
        elif got is not None and expected.startswith(got + PATH_SEPARATOR):
            score = ANCESTOR
        elif got is None:
            score = ABSTAINED
        else:
            score = WRONG
        return {
            "score": score,
            "exact": score == EXACT,
            "abstained": got is None and expected is not None,
            "wrong": score == WRONG and got is not None,
        }


def load_dataset(agent: str) -> Dataset:
    """The committed seed set for ``agent`` — versioned and PR-reviewed
    like tests."""
    return Dataset.from_file(
        EVALS_ROOT / agent / "seed.yaml", custom_evaluator_types=[CategoryScore]
    )


def categorization_task(model: str):
    """The measured task IS the production shape: same agent, same prompt
    builder, same degrade-to-abstain on exhausted retries."""

    async def task(inputs: dict) -> str | None:
        paths = inputs.get("taxonomy") or default_taxonomy_paths()
        prompt = format_prompt(
            payee=inputs["payee"],
            description=inputs["description"],
            amount_minor=inputs["amount_minor"],
            currency=inputs["currency"],
            date=inputs["date"],
            account_label=inputs["account_label"],
            account_kind=inputs["account_kind"],
            paths=paths,
        )
        try:
            result = await categorization_agent.run(prompt, deps=frozenset(paths), model=model)
        except Exception:
            return None
        return result.output.category_path

    return task


async def export_correction_log(out_path: Path) -> int:
    """A real ledger's correction log as eval cases — **user decisions
    only**: auto-filed entries are excluded by charter (they are the
    system applying precedent, not judgment worth learning from), and
    voided decisions are history, not truth. Exports stay local, never
    committed. Returns the number of cases written."""
    from pinch_backend.models import CorrectionActor, CorrectionKind, CorrectionLogEntry

    entries = await CorrectionLogEntry.where(
        lambda e: (e.actor == CorrectionActor.USER) & (e.kind == CorrectionKind.DECISION)
    ).all()
    voided_ids = {
        e.voids
        for e in await CorrectionLogEntry.where(lambda e: e.kind == CorrectionKind.VOID).all()
    }

    paths_by_ledger: dict[uuid.UUID, dict[str, uuid.UUID]] = {}
    cases: list[Case] = []
    for entry in entries:
        if entry.id in voided_ids or entry.input_payee is None:
            continue
        if entry.decision_splits is not None or entry.decision_transfer is not None:
            continue  # one category per case; split/transfer decisions aren't category truth
        ledger_id = entry.ledger_id  # ty: ignore[unresolved-attribute]
        if ledger_id not in paths_by_ledger:
            paths_by_ledger[ledger_id] = await taxonomy_paths(ledger_id)
        paths = paths_by_ledger[ledger_id]
        by_id = {category_id: path for path, category_id in paths.items()}
        expected = (
            by_id.get(entry.decision_category_id, entry.decision_category_name)
            if entry.decision_category_id is not None
            else None
        )
        cases.append(
            Case(
                name=f"log-{entry.id}",
                inputs={
                    "payee": entry.input_payee,
                    "description": entry.input_description_raw or entry.input_payee,
                    "amount_minor": entry.input_amount_minor or 0,
                    "currency": entry.input_currency or "USD",
                    "date": entry.input_date.isoformat() if entry.input_date else "",
                    "account_label": "unknown",
                    "account_kind": "depository",
                    "taxonomy": sorted(paths),
                },
                expected_output=expected,
            )
        )

    dataset = Dataset(name="correction-log-export", cases=cases, evaluators=[CategoryScore()])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_file(out_path, schema_path=None)
    return len(cases)
