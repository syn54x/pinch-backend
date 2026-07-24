"""M9 CP5: the mapping agent behind the inferrer seam (issue #59).

Layered, not replacing: the deterministic heuristic runs first, exactly as
today; Penny reads a bounded sample only when it abstains. Suggested
mappings were never authoritative, so the trust model doesn't move —
the user confirms or corrects either way. Keyless keeps today's behavior
byte-identical. FunctionModel only in CI.
"""

import io
import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pinch_backend.penny.mapping import mapping_agent

PASSWORD = "correct horse battery staple"

EASY_CSV = "Date,Amount,Description\n2026-07-01,-42.00,BLUE BOTTLE\n2026-07-02,-12.50,MUNI\n"
"""Headers straight out of the synonym lists: the heuristic maps this."""

WEIRD_CSV = (
    "When;How Much;What Happened\n"
    "Jul-01-26;(42.00);BLUE BOTTLE COFFEE\n"
    "Jul-02-26;(12.50);MUNI FARE\n"
    "Jul-03-26;1500.00;PAYROLL\n"
)
"""No synonym headers, a date format outside the trial list, parenthesized
negatives: the heuristic abstains on this shape."""

WEIRD_SPEC = {
    "delimiter": ";",
    "has_header": True,
    "date_column": 0,
    "date_format": "%b-%d-%y",
    "amount_column": 1,
    "sign": "negative_out",
    "description_columns": [2],
}


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


@pytest.fixture
async def uploader(client):
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": "taylor@example.com", "password": PASSWORD, "display_name": "T"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    account = (
        await client.post(
            "/api/v1/accounts",
            json={"kind": "depository", "label": "Checking", "currency": "USD"},
            headers=await _csrf(client),
        )
    ).json()

    async def upload(text: str):
        response = await client.post(
            "/api/v1/imports",
            data={"account_id": account["id"]},
            files={"file": ("export.csv", io.BytesIO(text.encode()), "text/csv")},
            headers=await _csrf(client),
        )
        assert response.status_code == 201, response.text
        return response.json()

    return upload


def _scripted(payloads: list[str]):
    calls = {"n": 0}

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        payload = payloads[min(calls["n"], len(payloads) - 1)]
        calls["n"] += 1
        return ModelResponse(
            parts=[ToolCallPart(tool_name="final_result", args=payload, tool_call_id="m1")]
        )

    return FunctionModel(script), calls


def _explode() -> FunctionModel:
    def script(messages, info):
        raise AssertionError("the mapping agent must not be consulted")

    return FunctionModel(script)


@pytest.fixture
def ai_enabled(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_mapping_model", "test")


async def test_heuristic_mappable_file_never_invokes_the_agent(uploader, ai_enabled) -> None:
    with mapping_agent.override(model=_explode()):
        body = await uploader(EASY_CSV)
    assert body["suggested_mapping"] is not None
    assert body["suggested_mapping"]["date_column"] == 0


async def test_weird_file_gets_pennys_suggestion(uploader, ai_enabled) -> None:
    model, calls = _scripted([json.dumps(WEIRD_SPEC)])
    with mapping_agent.override(model=model):
        body = await uploader(WEIRD_CSV)
    assert calls["n"] == 1
    suggestion = body["suggested_mapping"]
    assert suggestion is not None
    assert suggestion["delimiter"] == ";"
    assert suggestion["date_format"] == "%b-%d-%y"
    assert body["status"] == "uploaded"  # suggested, never auto-committed


async def test_invalid_spec_is_retried_then_recovers(uploader, ai_enabled) -> None:
    """A column index off the end of the row draws ModelRetry naming the
    problem; the corrected answer lands."""
    bad = json.dumps(WEIRD_SPEC | {"date_column": 9})
    model, calls = _scripted([bad, json.dumps(WEIRD_SPEC)])
    with mapping_agent.override(model=model):
        body = await uploader(WEIRD_CSV)
    assert calls["n"] == 2
    assert body["suggested_mapping"]["date_column"] == 0


async def test_persistent_nonsense_degrades_to_no_suggestion(uploader, ai_enabled) -> None:
    """Manual mapping — today's floor; the import flow is unchanged."""
    bad = json.dumps(WEIRD_SPEC | {"date_format": "%Q"})
    model, calls = _scripted([bad])
    with mapping_agent.override(model=model):
        body = await uploader(WEIRD_CSV)
    assert calls["n"] >= 2
    assert body["suggested_mapping"] is None
    assert body["status"] == "uploaded"


async def test_keyless_behavior_is_byte_identical_to_today(uploader) -> None:
    """No PINCH_AI_MAPPING_MODEL (the suite's baseline): weird files get no
    suggestion, and no model is ever consulted."""
    with mapping_agent.override(model=_explode()):
        easy = await uploader(EASY_CSV)
        weird = await uploader(WEIRD_CSV)
    assert easy["suggested_mapping"] is not None
    assert weird["suggested_mapping"] is None


async def test_retry_names_the_offending_column(uploader, ai_enabled) -> None:
    seen: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        retries = [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart)]
        if retries:
            seen.append(retries[0].model_response())
            payload = json.dumps(WEIRD_SPEC)
        else:
            payload = json.dumps(WEIRD_SPEC | {"amount_column": 7})
        return ModelResponse(
            parts=[ToolCallPart(tool_name="final_result", args=payload, tool_call_id="m1")]
        )

    with mapping_agent.override(model=FunctionModel(script)):
        body = await uploader(WEIRD_CSV)
    assert seen and "amount_column" in seen[0]
    assert body["suggested_mapping"]["amount_column"] == 1
