"""Penny's agents (PRD M9). Constructed model-less: the model is resolved
per run from the instance knobs (``settings.ai_*_model``) by the caller —
availability is checked first, so a run never starts on a broken knob.
Tests override with FunctionModel/TestModel via ``agent.override()``.
"""

from pydantic_ai import Agent

from pinch_backend.penny.bundles import read_bundle
from pinch_backend.penny.deps import PennyDeps
from pinch_backend.penny.prompts import CHAT_INSTRUCTIONS

chat_agent: Agent[PennyDeps, str] = Agent(
    deps_type=PennyDeps,
    instructions=CHAT_INSTRUCTIONS,
    capabilities=[read_bundle],
)
