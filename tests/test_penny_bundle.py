"""M9 CP1: the read bundle at the capability seam (issue #55).

Every tool produces its answer via the public v1 API, in-process, as the
chatting caller (ADR-0001 parity, extended to Penny). Exercised by running
the chat agent directly under a scripted FunctionModel that calls one tool
per test; assertions read the ToolReturnPart — the exact content the model
would see. The HTTP chat seam is tests/test_penny_chat.py's job.
"""

import json

import pytest
from litestar.testing import AsyncTestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pinch_backend.api.app import create_app
from pinch_backend.penny.agents import chat_agent
from pinch_backend.penny.deps import PennyDeps

PASSWORD = "correct horse battery staple"


@pytest.fixture
async def seeded(db):
    """A signed-up user with an account, a categorized transaction, and a
    rule — enough for every read tool to have something true to say."""
    app = create_app(manage_database=False)
    async with AsyncTestClient(app, base_url="https://testserver.local") as client:

        async def csrf() -> dict[str, str]:
            if "csrftoken" not in client.cookies:
                await client.get("/health")
            return {"x-csrftoken": client.cookies["csrftoken"]}

        response = await client.post(
            "/api/v1/auth/signup",
            json={"email": "taylor@example.com", "password": PASSWORD, "display_name": "T"},
            headers=await csrf(),
        )
        assert response.status_code == 201, response.text
        response = await client.post(
            "/api/v1/accounts",
            json={"kind": "depository", "label": "Penny Checking", "currency": "USD"},
            headers=await csrf(),
        )
        account = response.json()
        response = await client.get("/api/v1/categories")
        groceries = next(c for c in response.json()["items"] if c["name"] == "Groceries")
        response = await client.post(
            "/api/v1/transactions",
            json={
                "account_id": account["id"],
                "date": "2026-07-20",
                "amount_minor": -4200,
                "description": "COSTCO WHOLESALE",
                "category_id": groceries["id"],
            },
            headers=await csrf(),
        )
        assert response.status_code == 201, response.text
        txn = response.json()
        response = await client.post(
            "/api/v1/rules",
            json={
                "condition": {"payee": {"op": "contains", "value": "costco"}},
                "action_category_id": groceries["id"],
            },
            headers=await csrf(),
        )
        assert response.status_code == 201, response.text

        cookie = client.cookies[  # the session cookie, forwarded as the caller
            "pinch_session"
        ]
        deps = PennyDeps(app=app, auth_headers={"Cookie": f"pinch_session={cookie}"})
        yield {"app": app, "deps": deps, "account": account, "txn": txn}


def _call_tool_script(tool_name: str, args: dict):
    """A model that calls one tool, then stops."""

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if any(isinstance(p, ToolReturnPart) for p in last.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="c1")]
        )

    return FunctionModel(script)


async def _tool_return(seeded, tool_name: str, args: dict | None = None) -> str:
    with chat_agent.override(model=_call_tool_script(tool_name, args or {})):
        result = await chat_agent.run("go", deps=seeded["deps"])
    returns = [
        p
        for m in result.all_messages()
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart)
    ]
    assert returns, "tool was never called"
    content = returns[0].content
    return content if isinstance(content, str) else json.dumps(content)


async def test_list_accounts_reads_through_the_public_api(seeded) -> None:
    digest = await _tool_return(seeded, "list_accounts")
    assert "Penny Checking" in digest
    assert "depository" in digest


async def test_search_transactions_filters_and_digests(seeded) -> None:
    digest = await _tool_return(seeded, "search_transactions", {"query": "costco"})
    assert "COSTCO" in digest
    assert "-4200" in digest or "4200" in digest


async def test_get_transaction_answers_one_row(seeded) -> None:
    digest = await _tool_return(seeded, "get_transaction", {"transaction_id": seeded["txn"]["id"]})
    assert "COSTCO" in digest
    assert "Groceries" in digest


async def test_report_tools_answer_with_real_numbers(seeded) -> None:
    spending = await _tool_return(seeded, "spending_report", {"month": "2026-07"})
    assert "Groceries" in spending
    net_worth = await _tool_return(seeded, "net_worth_report")
    assert "net_worth" in net_worth or "total" in net_worth
    debt = await _tool_return(seeded, "debt_report")
    assert debt  # no loans seeded; an honest empty digest, not an error


async def test_taxonomy_and_rules_tools_answer(seeded) -> None:
    categories = await _tool_return(seeded, "list_categories")
    assert "Groceries" in categories
    rules = await _tool_return(seeded, "list_rules")
    assert "costco" in rules.lower()


async def test_recurring_and_stats_tools_answer(seeded) -> None:
    recurring = await _tool_return(seeded, "list_recurring_series")
    assert recurring  # nothing recurring seeded; honest empty answer
    stats = await _tool_return(seeded, "ledger_stats")
    assert "transactions_total" in stats


async def test_denied_self_call_is_reported_conversationally(seeded, monkeypatch) -> None:
    """A tool-level 403 becomes an honest sentence the model can relay,
    never a hidden retry or a raised 500 (PRD M9: tool-level 403s are
    reported conversationally)."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "verification_required", True)
    digest = await _tool_return(seeded, "list_accounts")
    assert "declined" in digest.lower()
    assert "verification" in digest.lower()
