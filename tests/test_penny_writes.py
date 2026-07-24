"""M9 CP2: writes and approvals from the wire (issue #56).

Every write capability pauses for an explicit in-conversation Approval:
approve executes and the effect is visible via the public API; deny leaves
zero trace; an unanswered approval expires — no durable state, no write,
and Penny says so if the topic resumes. All under scripted FunctionModels.
"""

import json
import uuid

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from pinch_backend.penny.agents import chat_agent

CHAT = "/api/v1/penny/chat"
PASSWORD = "correct horse battery staple"


async def _csrf(client) -> dict[str, str]:
    if "csrftoken" not in client.cookies:
        await client.get("/health")
    return {"x-csrftoken": client.cookies["csrftoken"]}


async def _signup(client, email: str = "taylor@example.com"):
    response = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Taylor"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text


def _submit_text(conversation_id: str, text: str) -> dict:
    return {
        "trigger": "submit-message",
        "id": conversation_id,
        "messages": [
            {"id": str(uuid.uuid7()), "role": "user", "parts": [{"type": "text", "text": text}]}
        ],
    }


def _submit_verdict(
    conversation_id: str,
    tool_name: str,
    tool_call_id: str,
    *,
    approved: bool,
    reason: str | None = None,
    args: dict | None = None,
) -> dict:
    """The follow-up request carrying the verdict — the assistant message
    with the approval-responded part, exactly as AI SDK v6 sends it."""
    approval: dict = {"id": tool_call_id, "approved": approved}
    if reason is not None:
        approval["reason"] = reason
    return {
        "trigger": "submit-message",
        "id": conversation_id,
        "messages": [
            {
                "id": str(uuid.uuid7()),
                "role": "assistant",
                "parts": [
                    {
                        "type": f"tool-{tool_name}",
                        "toolCallId": tool_call_id,
                        "state": "approval-responded",
                        "input": args or {},
                        "approval": approval,
                    }
                ],
            }
        ],
    }


def _sse_events(text: str) -> list:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: ") :]
            events.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return events


def _streamed_text(events: list) -> str:
    return "".join(
        e.get("delta", "") for e in events if isinstance(e, dict) and e.get("type") == "text-delta"
    )


def _approval_requests(events: list) -> list[dict]:
    return [e for e in events if isinstance(e, dict) and e.get("type") == "tool-approval-request"]


def _write_script(tool_name: str, args: dict):
    """Scripted model: ask for one write tool; then narrate the outcome —
    including 'never happened' when the history shows a denied/expired
    approval and the user asks again."""

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        last_returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if last_returns:
            r = last_returns[0]
            yield f"outcome[{r.outcome}]: {r.model_response_str()}"
            return
        past_denied = [
            p
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart) and p.outcome == "denied"
        ]
        if past_denied:
            yield f"never happened: {past_denied[0].model_response_str()}"
            return
        yield {0: DeltaToolCall(name=tool_name, json_args=json.dumps(args), tool_call_id="w1")}

    return stream


@pytest.fixture
def chat_enabled(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_chat_model", "test")


async def _category_names(client) -> list[str]:
    body = (await client.get("/api/v1/categories", params={"limit": 100})).json()
    return [c["name"] for c in body["items"]]


CREATE_COFFEE = _write_script("create_category", {"name": "Coffee Shops"})


async def test_approved_write_takes_effect_only_after_approval(client, chat_enabled) -> None:
    await _signup(client)
    conversation_id = str(uuid.uuid7())

    with chat_agent.override(model=FunctionModel(stream_function=CREATE_COFFEE)):
        first = await client.post(
            CHAT,
            json=_submit_text(conversation_id, "Make a Coffee Shops category"),
            headers=await _csrf(client),
        )
        assert first.status_code == 200, first.text
        events = _sse_events(first.text)
        requests = _approval_requests(events)
        assert requests and requests[0]["toolCallId"] == "w1"
        # The pause is real: nothing is written while approval is pending.
        assert "Coffee Shops" not in await _category_names(client)

        second = await client.post(
            CHAT,
            json=_submit_verdict(
                conversation_id,
                "create_category",
                "w1",
                approved=True,
                args={"name": "Coffee Shops"},
            ),
            headers=await _csrf(client),
        )
        assert second.status_code == 200, second.text

    text = _streamed_text(_sse_events(second.text))
    assert "outcome[success]" in text
    assert "Coffee Shops" in await _category_names(client)

    # Reload renders the resolved approval, not a stuck pending state.
    body = (await client.get(f"/api/v1/penny/conversations/{conversation_id}")).json()
    states = [
        p.get("state")
        for m in body["messages"]
        for p in m["parts"]
        if p.get("type") == "tool-create_category"
    ]
    assert "output-available" in states


async def test_denied_write_leaves_zero_trace(client, chat_enabled) -> None:
    await _signup(client)
    conversation_id = str(uuid.uuid7())

    with chat_agent.override(model=FunctionModel(stream_function=CREATE_COFFEE)):
        await client.post(
            CHAT,
            json=_submit_text(conversation_id, "Make a Coffee Shops category"),
            headers=await _csrf(client),
        )
        second = await client.post(
            CHAT,
            json=_submit_verdict(
                conversation_id,
                "create_category",
                "w1",
                approved=False,
                reason="wrong name, never mind",
                args={"name": "Coffee Shops"},
            ),
            headers=await _csrf(client),
        )
        assert second.status_code == 200, second.text

    text = _streamed_text(_sse_events(second.text))
    assert "outcome[denied]" in text
    assert "wrong name" in text
    assert "Coffee Shops" not in await _category_names(client)


async def test_unanswered_approval_expires_and_penny_says_so(client, chat_enabled) -> None:
    """The stream died with the approval pending: no durable pending state,
    no write — and when the topic resumes, the action never happened."""
    await _signup(client)
    conversation_id = str(uuid.uuid7())

    with chat_agent.override(model=FunctionModel(stream_function=CREATE_COFFEE)):
        await client.post(
            CHAT,
            json=_submit_text(conversation_id, "Make a Coffee Shops category"),
            headers=await _csrf(client),
        )
        resumed = await client.post(
            CHAT,
            json=_submit_text(conversation_id, "did you create it?"),
            headers=await _csrf(client),
        )
        assert resumed.status_code == 200, resumed.text

    text = _streamed_text(_sse_events(resumed.text))
    # The denial reached the model (as a denied tool return merged into the
    # resume request) and was relayed to the user.
    assert "never taken" in text
    assert "Coffee Shops" not in await _category_names(client)


async def test_accept_review_via_penny_is_a_user_decision(client, chat_enabled) -> None:
    """Same actor weight as the inbox's Accept button: the user approved it."""
    await _signup(client)
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
                "date": "2026-07-21",
                "amount_minor": -999,
                "description": "MYSTERY VENDOR",
            },
            headers=await _csrf(client),
        )
    ).json()
    assert txn["reviewed_at"] is None

    conversation_id = str(uuid.uuid7())
    script = _write_script("accept_review", {"transaction_id": txn["id"]})
    with chat_agent.override(model=FunctionModel(stream_function=script)):
        await client.post(
            CHAT,
            json=_submit_text(conversation_id, "accept that mystery transaction"),
            headers=await _csrf(client),
        )
        second = await client.post(
            CHAT,
            json=_submit_verdict(
                conversation_id,
                "accept_review",
                "w1",
                approved=True,
                args={"transaction_id": txn["id"]},
            ),
            headers=await _csrf(client),
        )
    assert "outcome[success]" in _streamed_text(_sse_events(second.text))

    refreshed = (await client.get(f"/api/v1/transactions/{txn['id']}")).json()
    assert refreshed["reviewed_at"] is not None

    log_body = (await client.get("/api/v1/correction-log")).json()
    entries = [e for e in log_body["items"] if e["transaction_id"] == txn["id"]]
    assert entries and all(e["actor"] == "user" for e in entries)


async def test_scope_limited_writer_hears_the_denial(client, chat_enabled) -> None:
    """A read+penny token approves the write, the API refuses it, and the
    refusal arrives as conversation — never a silent failure."""
    await _signup(client)
    token = (
        await client.post(
            "/api/v1/auth/pats",
            json={"name": "reader", "scopes": ["read", "penny"]},
            headers=await _csrf(client),
        )
    ).json()["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    conversation_id = str(uuid.uuid7())

    with chat_agent.override(model=FunctionModel(stream_function=CREATE_COFFEE)):
        await client.post(
            CHAT, json=_submit_text(conversation_id, "make the category"), headers=bearer
        )
        second = await client.post(
            CHAT,
            json=_submit_verdict(
                conversation_id,
                "create_category",
                "w1",
                approved=True,
                args={"name": "Coffee Shops"},
            ),
            headers=bearer,
        )
        assert second.status_code == 200, second.text

    text = _streamed_text(_sse_events(second.text))
    assert "outcome[success]" in text  # the tool ran; its honest answer is the refusal
    assert "declined" in text.lower()
    assert "Coffee Shops" not in await _category_names(client)
