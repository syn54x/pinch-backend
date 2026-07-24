"""M9 CP1: the chat seam, end-to-end from the wire (issue #55).

POST /api/v1/penny/chat speaks the Vercel AI Data Stream protocol (SSE,
sdk_version 6) via the CP0-proven Litestar glue, under a scripted
FunctionModel — no live LLM, ever. Conversations persist server-side and
resume; the penny scope gates tokens; keyless declines cleanly.
"""

import json
import uuid

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from pinch_backend.models import Conversation
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


async def _seed_account(client, label: str = "Penny Checking"):
    response = await client.post(
        "/api/v1/accounts",
        json={"kind": "depository", "label": label, "currency": "USD"},
        headers=await _csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _submit(conversation_id: str, text: str) -> dict:
    return {
        "trigger": "submit-message",
        "id": conversation_id,
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": text}]}],
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


async def _grounded_stream(messages: list[ModelMessage], info: AgentInfo):
    """Script: read the accounts, then answer from the tool result."""
    last = messages[-1]
    returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
    if returns:
        yield f"You have: {returns[0].content}"
    else:
        yield {0: DeltaToolCall(name="list_accounts", json_args="{}", tool_call_id="t1")}


async def _counting_stream(messages: list[ModelMessage], info: AgentInfo):
    """Script: answer with how much history reached the model — the
    resumability probe."""
    yield f"messages_seen:{len(messages)}"


@pytest.fixture
def chat_enabled(monkeypatch):
    """Availability on (the 'test' model resolves keylessly); the actual
    model behavior is overridden per-test."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "ai_chat_model", "test")


async def test_keyless_chat_declines_cleanly_with_the_reason(client) -> None:
    await _signup(client)
    response = await client.post(
        CHAT, json=_submit(str(uuid.uuid7()), "hi"), headers=await _csrf(client)
    )
    assert response.status_code == 503
    assert "PINCH_AI_CHAT_MODEL" in response.json()["detail"]
    assert await Conversation.all() == []


async def test_chat_streams_a_grounded_answer_and_persists(client, chat_enabled) -> None:
    await _signup(client)
    await _seed_account(client)
    conversation_id = str(uuid.uuid7())

    with chat_agent.override(model=FunctionModel(stream_function=_grounded_stream)):
        response = await client.post(
            CHAT,
            json=_submit(conversation_id, "What accounts do I have?"),
            headers=await _csrf(client),
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    assert "Penny Checking" in _streamed_text(events)
    assert events[-1] == "[DONE]"

    row = await Conversation.where(lambda c: c.id == uuid.UUID(conversation_id)).first()
    assert row is not None
    assert row.title == "What accounts do I have?"
    kinds = [m["kind"] for m in row.messages]
    assert kinds == ["request", "response", "request", "response"]

    body = (await client.get(f"/api/v1/penny/conversations/{conversation_id}")).json()
    assert body["messages"][-1]["parts"][-1]["text"].startswith("You have:")


async def test_conversation_resumes_with_server_history(client, chat_enabled) -> None:
    """The server's record is authoritative: round two sends only the new
    message, and the model still sees the whole thread."""
    await _signup(client)
    conversation_id = str(uuid.uuid7())

    with chat_agent.override(model=FunctionModel(stream_function=_counting_stream)):
        first = await client.post(
            CHAT, json=_submit(conversation_id, "first"), headers=await _csrf(client)
        )
        assert "messages_seen:1" in _streamed_text(_sse_events(first.text))

        second = await client.post(
            CHAT, json=_submit(conversation_id, "second"), headers=await _csrf(client)
        )
        assert "messages_seen:3" in _streamed_text(_sse_events(second.text))

    row = await Conversation.where(lambda c: c.id == uuid.UUID(conversation_id)).first()
    assert len(row.messages) == 4  # two turns, each request + response
    assert row.title == "first"  # the title never chases later messages


async def test_pat_without_penny_scope_cannot_chat(client, chat_enabled) -> None:
    await _signup(client)
    response = await client.post(
        "/api/v1/auth/pats",
        json={"name": "no-penny", "scopes": ["read", "write"]},
        headers=await _csrf(client),
    )
    token = response.json()["token"]
    response = await client.post(
        CHAT,
        json=_submit(str(uuid.uuid7()), "hi"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert "penny" in response.json()["detail"].lower()


async def test_pat_with_penny_scope_chats_as_itself(client, chat_enabled) -> None:
    await _signup(client)
    await _seed_account(client, label="Bearer Checking")
    response = await client.post(
        "/api/v1/auth/pats",
        json={"name": "chatty", "scopes": ["read", "penny"]},
        headers=await _csrf(client),
    )
    token = response.json()["token"]

    with chat_agent.override(model=FunctionModel(stream_function=_grounded_stream)):
        response = await client.post(
            CHAT,
            json=_submit(str(uuid.uuid7()), "accounts?"),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    assert "Bearer Checking" in _streamed_text(_sse_events(response.text))


async def test_conversation_id_must_be_uuid7(client, chat_enabled) -> None:
    await _signup(client)
    for bad in ("chat-1", str(uuid.uuid4())):
        response = await client.post(CHAT, json=_submit(bad, "hi"), headers=await _csrf(client))
        assert response.status_code == 400, bad
        assert "UUIDv7" in response.json()["detail"]


async def test_foreign_conversation_id_is_refused(client, chat_enabled) -> None:
    """A conversation id owned by another ledger answers the ownership
    404 — never a cross-ledger append, never a pk crash."""
    await _signup(client, email="alice@example.com")
    conversation_id = str(uuid.uuid7())
    with chat_agent.override(model=FunctionModel(stream_function=_counting_stream)):
        await client.post(
            CHAT, json=_submit(conversation_id, "alice's"), headers=await _csrf(client)
        )
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))

    await _signup(client, email="bob@example.com")
    with chat_agent.override(model=FunctionModel(stream_function=_counting_stream)):
        response = await client.post(
            CHAT, json=_submit(conversation_id, "bob's probe"), headers=await _csrf(client)
        )
    assert response.status_code == 404
    row = await Conversation.where(lambda c: c.id == uuid.UUID(conversation_id)).first()
    assert row.title == "alice's"
    assert len(row.messages) == 2
