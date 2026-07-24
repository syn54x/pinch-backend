"""M9 CP3: the categorization agent behind the classifier seam (issue #57).

Zero pipeline change: `provenance: ai` simply becomes reachable when rules
and history miss. Hallucinated categories draw ModelRetry; persistent
nonsense degrades to abstain — never a bad write. Keyless abstains,
deterministically, exactly today's behavior. FunctionModel only in CI.
"""

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pinch_backend.penny.categorization import categorization_agent

PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


@pytest.fixture
async def ledger_with_txn(client):
    """A signed-up user, an account, and one unreviewed transaction whose
    payee no rule or history entry knows — the AI stage's case."""
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
    txn = (
        await client.post(
            "/api/v1/transactions",
            json={
                "account_id": account["id"],
                "date": "2026-07-22",
                "amount_minor": -1850,
                "description": "BLUE BOTTLE COFFEE OAK-3",
            },
            headers=await _csrf(client),
        )
    ).json()
    assert txn["reviewed_at"] is None
    return {"client": client, "txn": txn}


def _structured_response(path_json: str) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name="final_result", args=path_json, tool_call_id="fr1")]
    )


def _scripted(paths: list[str]):
    """A model that answers the structured output tool with each payload in
    turn (retries advance the list)."""
    calls = {"n": 0}

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        payload = paths[min(calls["n"], len(paths) - 1)]
        calls["n"] += 1
        return _structured_response(payload)

    return FunctionModel(script), calls


@pytest.fixture
def ai_enabled(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_categorization_model", "test")


async def _proposal_of(client, txn_id: str) -> dict | None:
    body = (await client.get(f"/api/v1/transactions/{txn_id}")).json()
    return body["proposal"]


async def test_unmatched_transaction_gets_an_ai_proposal(
    ledger_with_txn, run_jobs, ai_enabled
) -> None:
    client, txn = ledger_with_txn["client"], ledger_with_txn["txn"]
    model, _ = _scripted(['{"category_path": "Food & Drink > Coffee"}'])
    with categorization_agent.override(model=model):
        await run_jobs()

    proposal = await _proposal_of(client, txn["id"])
    assert proposal is not None
    assert proposal["provenance"] == "ai"
    assert proposal["category"]["name"] == "Coffee"


async def test_hallucinated_category_is_retried_then_recovers(
    ledger_with_txn, run_jobs, ai_enabled
) -> None:
    """The first answer names a category that doesn't exist; ModelRetry
    surfaces it and the corrected second answer lands."""
    client, txn = ledger_with_txn["client"], ledger_with_txn["txn"]
    model, calls = _scripted(
        ['{"category_path": "Beverages > Espresso"}', '{"category_path": "Food & Drink > Coffee"}']
    )
    with categorization_agent.override(model=model):
        await run_jobs()

    assert calls["n"] == 2
    proposal = await _proposal_of(client, txn["id"])
    assert proposal["provenance"] == "ai"
    assert proposal["category"]["name"] == "Coffee"


async def test_persistent_nonsense_degrades_to_abstain(
    ledger_with_txn, run_jobs, ai_enabled
) -> None:
    """Retries exhausted on hallucinated paths → uncategorized with
    provenance none — never a bad write, never a crashed sweep."""
    client, txn = ledger_with_txn["client"], ledger_with_txn["txn"]
    model, calls = _scripted(['{"category_path": "Beverages > Espresso"}'])
    with categorization_agent.override(model=model):
        await run_jobs()

    assert calls["n"] >= 2  # it did retry before giving up
    proposal = await _proposal_of(client, txn["id"])
    assert proposal is not None
    assert proposal["provenance"] == "none"
    assert proposal["category"] is None


async def test_keyless_instance_abstains_without_calling_any_model(
    ledger_with_txn, run_jobs
) -> None:
    """No PINCH_AI_CATEGORIZATION_MODEL (the suite's baseline): the stage
    abstains deterministically and no model is ever consulted."""
    client, txn = ledger_with_txn["client"], ledger_with_txn["txn"]

    def explode(messages, info):
        raise AssertionError("keyless classify must not reach a model")

    with categorization_agent.override(model=FunctionModel(explode)):
        await run_jobs()

    proposal = await _proposal_of(client, txn["id"])
    assert proposal is not None
    assert proposal["provenance"] == "none"


async def test_model_seeing_retry_gets_the_reason(ledger_with_txn, run_jobs, ai_enabled) -> None:
    """The retry prompt names the hallucinated path so the model can
    correct course."""
    seen: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        retries = [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart)]
        if retries:
            seen.append(retries[0].model_response())
            return _structured_response('{"category_path": null}')
        return _structured_response('{"category_path": "Nope > Nada"}')

    with categorization_agent.override(model=FunctionModel(script)):
        await run_jobs()

    assert seen and "Nope > Nada" in seen[0]
    proposal = await _proposal_of(ledger_with_txn["client"], ledger_with_txn["txn"]["id"])
    assert proposal["provenance"] == "none"
