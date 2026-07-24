"""M9 CP1: Conversations over the public HTTP seam (issue #55).

Server history is authoritative and ledger-owned: list (newest-first,
cursor-paginated), get (messages rendered in Vercel AI UI format for F6's
reload), delete. No create route — POST /penny/chat mints conversations.
"""

import uuid

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from pinch_backend.models import Conversation, Ledger, LedgerMember, User

CONVERSATIONS = "/api/v1/penny/conversations"
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


async def _ledger_of(email: str) -> Ledger:
    user = await User.where(lambda u: u.email == email).first()
    membership = await LedgerMember.where(lambda m: m.user_id == user.id).first()
    return await Ledger.get(membership.ledger_id)


def _native_messages(user_text: str, reply: str) -> list[dict]:
    history = [
        ModelRequest(parts=[UserPromptPart(content=user_text)]),
        ModelResponse(parts=[TextPart(content=reply)]),
    ]
    return ModelMessagesTypeAdapter.dump_python(history, mode="json")


async def _seed(ledger: Ledger, title: str, user_text: str = "hi", reply: str = "hello"):
    return await Conversation.create(
        ledger=ledger, title=title, messages=_native_messages(user_text, reply)
    )


async def test_list_answers_the_envelope_and_is_empty_at_birth(client) -> None:
    await _signup(client)
    body = (await client.get(CONVERSATIONS)).json()
    assert body == {"items": [], "next_cursor": None}


async def test_list_is_newest_first_and_paginates_on_the_convention(client) -> None:
    await _signup(client)
    ledger = await _ledger_of("taylor@example.com")
    for i in range(5):
        await _seed(ledger, title=f"conversation {i}")

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get(CONVERSATIONS, params=params)).json()
        assert len(body["items"]) <= 2
        seen += [item["title"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"conversation {i}" for i in reversed(range(5))]


async def test_get_renders_messages_in_vercel_ui_format(client) -> None:
    """F6 reloads a conversation straight into useChat: the server answers
    UI messages, not pydantic-ai internals."""
    await _signup(client)
    ledger = await _ledger_of("taylor@example.com")
    row = await _seed(ledger, title="about coffee", user_text="coffee spend?", reply="$42")

    body = (await client.get(f"{CONVERSATIONS}/{row.id}")).json()
    assert body["id"] == str(row.id)
    assert body["title"] == "about coffee"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    (user_part,) = body["messages"][0]["parts"]
    assert (user_part["type"], user_part["text"]) == ("text", "coffee spend?")
    assert body["messages"][1]["parts"][0]["text"] == "$42"


async def test_conversations_are_ledger_scoped_with_the_same_404(client) -> None:
    await _signup(client, email="alice@example.com")
    alice_ledger = await _ledger_of("alice@example.com")
    alice_row = await _seed(alice_ledger, title="alice's")
    await client.post("/api/v1/auth/logout", headers=await _csrf(client))

    await _signup(client, email="bob@example.com")
    assert (await client.get(CONVERSATIONS)).json()["items"] == []
    assert (await client.get(f"{CONVERSATIONS}/{alice_row.id}")).status_code == 404
    response = await client.delete(f"{CONVERSATIONS}/{alice_row.id}", headers=await _csrf(client))
    assert response.status_code == 404
    assert await Conversation.where(lambda c: c.id == alice_row.id).first() is not None


async def test_delete_removes_the_conversation(client) -> None:
    await _signup(client)
    ledger = await _ledger_of("taylor@example.com")
    row = await _seed(ledger, title="doomed")

    response = await client.delete(f"{CONVERSATIONS}/{row.id}", headers=await _csrf(client))
    assert response.status_code == 204
    assert (await client.get(f"{CONVERSATIONS}/{row.id}")).status_code == 404
    assert await Conversation.where(lambda c: c.id == row.id).first() is None


async def test_unknown_conversation_is_404_not_500(client) -> None:
    await _signup(client)
    assert (await client.get(f"{CONVERSATIONS}/{uuid.uuid7()}")).status_code == 404
