"""Keyless degradation as a first-class state (PRD M9): an agent is
*available* when its model knob is set and resolvable — provider key
present, identifier well-formed. Anything less is disabled-with-a-reason,
never an error: chat declines cleanly, the classifier abstains, the
mapping heuristic stands alone.

Resolution goes through pydantic-ai's own ``infer_model`` so the reason is
the provider's actual complaint (e.g. which env var is missing), not a
guess — and so "resolvable here" and "runnable here" can never drift.
"""

from pydantic import BaseModel

from pinch_backend.settings import settings

AGENT_KNOBS = {
    "chat": "PINCH_AI_CHAT_MODEL",
    "categorization": "PINCH_AI_CATEGORIZATION_MODEL",
    "mapping": "PINCH_AI_MAPPING_MODEL",
}


class AgentAvailability(BaseModel):
    available: bool
    reason: str | None = None
    """Human-readable and stable enough to display (F6 renders it);
    never carries secret material."""


def _resolve(model_string: str, knob: str) -> AgentAvailability:
    if not model_string:
        return AgentAvailability(available=False, reason=f"{knob} is not set")
    from pydantic_ai.models import infer_model

    try:
        infer_model(model_string)
    except Exception as error:
        return AgentAvailability(available=False, reason=str(error))
    return AgentAvailability(available=True)


def chat_availability() -> AgentAvailability:
    return _resolve(settings.ai_chat_model, AGENT_KNOBS["chat"])


def categorization_availability() -> AgentAvailability:
    return _resolve(settings.ai_categorization_model, AGENT_KNOBS["categorization"])


def mapping_availability() -> AgentAvailability:
    return _resolve(settings.ai_mapping_model, AGENT_KNOBS["mapping"])
