"""The AI-classification seam (PRD M5 D12): a protocol the pipeline depends
on — the MappingInferrer precedent exactly. M9 CP3 swapped Penny's
categorization agent in behind it: keyless instances (CI's baseline) still
abstain deterministically without touching a model, so "no LLM, no keys,
no network in CI" holds exactly as it did under the abstainer.
"""

from typing import TYPE_CHECKING, Protocol

from pinch_backend.penny.categorization import PennyClassifier

if TYPE_CHECKING:
    import uuid

    from pinch_backend.models import Transaction


class Classifier(Protocol):
    async def classify(self, txn: "Transaction") -> "uuid.UUID | None":
        """A category id for ``txn``, or None to abstain; the pipeline never
        asks how."""
        ...


class AbstainingClassifier:
    """The pre-M9 default, kept for tests that want the seam inert."""

    async def classify(self, txn: "Transaction") -> "uuid.UUID | None":
        return None


active_classifier: Classifier = PennyClassifier()
