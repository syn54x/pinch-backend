"""The categorization agent (PRD M9 CP3): fills the M5 classifier seam
with zero pipeline change — ``provenance: ai`` simply becomes reachable.

Input is the transaction's facts plus the user's taxonomy as full paths
(the closed answer set); output is one of those paths or an explicit
abstain. A hallucinated path draws ModelRetry with the offending value;
exhausted retries degrade to abstain — a wrong label costs more trust than
a missing one, so the failure mode is silence, never a bad write. Keyless
abstains without constructing anything.

Prompt v1 is taxonomy-only. Correction-log few-shots are the harness's
first *experiment* (they only help novel payees — exact repeats are the
history stage's job), added when eval evidence justifies them.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext

from pinch_backend.models import Account, Category, Transaction

if TYPE_CHECKING:
    import uuid
from pinch_backend.observability import get_logger
from pinch_backend.penny.availability import categorization_availability
from pinch_backend.settings import settings

log = get_logger(__name__)

PATH_SEPARATOR = " > "

CATEGORIZATION_INSTRUCTIONS = """\
You classify one personal-finance transaction into the user's own category
taxonomy.

Rules:
- Answer with exactly one category path from the provided taxonomy,
  verbatim — or null to abstain.
- Abstain when you are not reasonably sure. An honest "uncategorized" is
  worth more than a plausible guess: the user reviews every proposal, and
  a wrong label costs more trust than a missing one.
- Amounts are integer minor units; negative is money out of the account.
- Never invent categories. The taxonomy provided is the entire answer set.
"""


class Categorization(BaseModel):
    """The structured verdict: a verbatim taxonomy path, or null to
    abstain. No confidence scores — abstention is the model's judgment,
    and evals measure its calibration."""

    category_path: str | None


# ty can't thread output_type through Agent's constructor overloads; the
# annotation states what output_type=Categorization already enforces.
categorization_agent: Agent[frozenset[str], Categorization] = Agent(  # ty: ignore[invalid-assignment]
    deps_type=frozenset[str],
    output_type=Categorization,
    instructions=CATEGORIZATION_INSTRUCTIONS,
)


@categorization_agent.output_validator
def _path_must_exist(ctx: RunContext[frozenset[str]], output: Categorization) -> Categorization:
    if output.category_path is not None and output.category_path not in ctx.deps:
        raise ModelRetry(
            f"{output.category_path!r} is not a path in the taxonomy. Answer with "
            "one of the provided paths verbatim, or null to abstain."
        )
    return output


async def taxonomy_paths(ledger_id: "uuid.UUID") -> dict[str, uuid.UUID]:
    """Full paths ("Food & Drink > Coffee") to category ids, leaf and
    parent alike — every category is a legal answer."""
    categories = await Category.where(lambda c: c.ledger_id == ledger_id).all()
    by_id = {c.id: c for c in categories}
    paths: dict[str, uuid.UUID] = {}
    for category in categories:
        parts = [category.name]
        parent_id = category.parent_id  # ty: ignore[unresolved-attribute]
        while parent_id is not None:
            parent = by_id[parent_id]
            parts.append(parent.name)
            parent_id = parent.parent_id  # ty: ignore[unresolved-attribute]
        paths[PATH_SEPARATOR.join(reversed(parts))] = category.id
    return paths


def format_prompt(
    *,
    payee: str,
    description: str,
    amount_minor: int,
    currency: str,
    date: str,
    account_label: str,
    account_kind: str,
    paths: "list[str] | dict[str, uuid.UUID]",
) -> str:
    """The one prompt shape, shared by the classifier and the evals
    harness so measured behavior and production behavior cannot drift."""
    taxonomy = "\n".join(f"- {path}" for path in sorted(paths))
    return (
        f"Transaction:\n"
        f"- payee (normalized): {payee}\n"
        f"- raw description: {description}\n"
        f"- amount_minor: {amount_minor} {currency}\n"
        f"- date: {date}\n"
        f"- account: {account_label} ({account_kind})\n\n"
        f"Taxonomy (the complete answer set):\n{taxonomy}"
    )


class PennyClassifier:
    """The seam implementation (classification/classifier.py protocol).
    Every failure shape — keyless, empty taxonomy, exhausted retries,
    provider trouble — is the same honest answer: abstain."""

    async def classify(self, txn: "Transaction") -> "uuid.UUID | None":
        if not categorization_availability().available:
            return None
        paths = await taxonomy_paths(txn.ledger_id)  # ty: ignore[unresolved-attribute]
        if not paths:
            return None
        account = await Account.get(txn.account_id)  # ty: ignore[unresolved-attribute]
        prompt = format_prompt(
            payee=txn.description_normalized,
            description=txn.description_raw,
            amount_minor=txn.amount_minor,
            currency=txn.currency,
            date=txn.date.isoformat(),
            account_label=account.label,
            account_kind=account.kind.value,
            paths=paths,
        )
        try:
            result = await categorization_agent.run(
                prompt,
                deps=frozenset(paths),
                model=settings.ai_categorization_model,
            )
        except Exception as error:
            log.warning(
                "penny.categorization.abstained",
                transaction_id=str(txn.id),
                error=str(error),
            )
            return None
        path = result.output.category_path
        return paths[path] if path is not None else None
