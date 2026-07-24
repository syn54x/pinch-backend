"""Penny's agents (PRD M9). Constructed model-less: the model is resolved
per run from the instance knobs (``settings.ai_*_model``) by the caller —
availability is checked first, so a run never starts on a broken knob.
Tests override with FunctionModel/TestModel via ``agent.override()``.
"""

from pydantic_ai import Agent, DeferredToolRequests

from pinch_backend.penny.bundles import read_bundle, write_bundle
from pinch_backend.penny.deps import PennyDeps
from pinch_backend.penny.prompts import CHAT_INSTRUCTIONS

chat_agent: Agent[PennyDeps, str | DeferredToolRequests] = Agent(
    deps_type=PennyDeps,
    output_type=[str, DeferredToolRequests],
    instructions=CHAT_INSTRUCTIONS,
    capabilities=[read_bundle, write_bundle],
)
"""Composes both bundles (PRD M9): a future read-only consumer just omits
the writes bundle. DeferredToolRequests in the output union is what lets a
run pause on a write approval instead of erroring."""
