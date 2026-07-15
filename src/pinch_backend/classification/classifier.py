"""The AI-classification seam (PRD M5 D12): a protocol the pipeline depends
on and a deterministic abstainer behind it — the MappingInferrer precedent
exactly. No LLM, no keys, no network: the abstainer is what guarantees CI
never talks to the outside world, and provenance=ai stays unreachable until
M9 swaps Penny in behind the same protocol.
"""

import uuid  # noqa: TC003
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pinch_backend.models import Transaction


class Classifier(Protocol):
    async def classify(self, txn: "Transaction") -> uuid.UUID | None:
        """A category id for ``txn``, or None to abstain; the pipeline never
        asks how."""
        ...


class AbstainingClassifier:
    """v0: always abstains, deterministically."""

    async def classify(self, txn: "Transaction") -> uuid.UUID | None:
        return None


active_classifier: Classifier = AbstainingClassifier()
