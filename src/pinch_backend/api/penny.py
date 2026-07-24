"""/api/v1/penny — Penny's public surface (PRD M9, CP1 #55).

Conversations are server-persisted and authoritative: the client contributes
new messages through the chat endpoint; these routes let it list, reload,
and delete what the server holds. Stored history is pydantic-ai native JSON
(the lossless CP0 round-trip); reads render it in Vercel AI UI format so F6
reloads straight into useChat.
"""

import json
import uuid
from datetime import datetime

from litestar import Request, Router, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import (
    ClientException,
    HTTPException,
    NotFoundException,
    PermissionDeniedException,
)
from litestar.params import FromPath
from litestar.response import Stream
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from pydantic_ai.ui.vercel_ai.request_types import TextUIPart

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate_desc,
)
from pinch_backend.auth.guards import Credential
from pinch_backend.models import Conversation, Ledger, User
from pinch_backend.observability import get_logger
from pinch_backend.penny.agents import chat_agent
from pinch_backend.penny.availability import (
    AgentAvailability,
    categorization_availability,
    chat_availability,
    mapping_availability,
)
from pinch_backend.penny.deps import PennyDeps
from pinch_backend.settings import settings

log = get_logger(__name__)

_TITLE_LENGTH = 80


class PennyStatusOut(BaseModel):
    """What F6 renders keyless/disabled states from. The top level is the
    chat agent — "is Penny here?" — with the per-agent detail alongside."""

    available: bool
    reason: str | None
    agents: dict[str, AgentAvailability]


@get("/status")
async def penny_status(current_user: NamedDependency[User]) -> PennyStatusOut:
    """Credentialed (any signed-in user or PAT): instance AI configuration
    is not anonymous information. No ledger dependency — availability is
    instance-level, not data."""
    chat = chat_availability()
    return PennyStatusOut(
        available=chat.available,
        reason=chat.reason,
        agents={
            "chat": chat,
            "categorization": categorization_availability(),
            "mapping": mapping_availability(),
        },
    )


class ConversationSummaryOut(BaseModel):
    """One row of the conversation list — no messages; reloading a thread
    is the get endpoint's job."""

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationOut(ConversationSummaryOut):
    """The full thread. ``messages`` is Vercel AI UI-message JSON
    (sdk_version 6), the format useChat renders — including
    approval-requested states on reload."""

    messages: list[dict]


async def _get_conversation(ledger: Ledger, conversation_id: uuid.UUID) -> Conversation:
    """Fetch within the acting ledger: another ledger's conversation answers
    the same 404 as a nonexistent one — never a confirming 403."""
    row = await Conversation.where(
        lambda c: (c.id == conversation_id) & (c.ledger_id == ledger.id)
    ).first()
    if row is None:
        raise NotFoundException(detail="No such conversation")
    return row


def _ui_messages(row: Conversation) -> list[dict]:
    history = ModelMessagesTypeAdapter.validate_python(row.messages)
    dumped = VercelAIAdapter.dump_messages(history, sdk_version=6)
    return [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in dumped]


@get("/conversations")
async def list_conversations(
    current_ledger: NamedDependency[Ledger],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[ConversationSummaryOut]:
    """Newest-first — a chat list leads with the live thread."""
    ledger_id = current_ledger.id
    rows, next_cursor = await paginate_desc(
        Conversation.where(lambda c: c.ledger_id == ledger_id), cursor=cursor, limit=limit
    )
    return Page(
        items=[
            ConversationSummaryOut(
                id=c.id, title=c.title, created_at=c.created_at, updated_at=c.updated_at
            )
            for c in rows
        ],
        next_cursor=next_cursor,
    )


@get("/conversations/{conversation_id:uuid}")
async def get_conversation(
    conversation_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> ConversationOut:
    row = await _get_conversation(current_ledger, conversation_id)
    return ConversationOut(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        messages=_ui_messages(row),
    )


@delete("/conversations/{conversation_id:uuid}")
async def delete_conversation(
    conversation_id: FromPath[uuid.UUID], current_ledger: NamedDependency[Ledger]
) -> None:
    row = await _get_conversation(current_ledger, conversation_id)
    await row.delete()
    log.info(
        "penny.conversation.deleted",
        conversation_id=str(row.id),
        ledger_id=str(current_ledger.id),
    )


def _conversation_uuid(raw: str) -> uuid.UUID:
    """The chat id is the conversation id, client-minted as UUIDv7 so the
    id-keyset list convention keeps creation order (route docstring)."""
    try:
        conversation_id = uuid.UUID(raw)
    except ValueError, TypeError:
        conversation_id = None
    if conversation_id is None or conversation_id.version != 7:
        raise ClientException(detail="Conversation id must be a UUIDv7")
    return conversation_id


def _caller_headers(request: Request) -> dict[str, str]:
    """The chatting caller's own credential, verbatim (PRD M9: tools run as
    the caller). Bearer wins when both are present — the guard's invariant,
    mirrored so the self-call resolves the same principal."""
    authorization = request.headers.get("authorization", "")
    if authorization.split(" ", 1)[0].lower() == "bearer":
        return {"Authorization": authorization}
    cookie = request.cookies.get(settings.session_cookie_name, "")
    return {"Cookie": f"{settings.session_cookie_name}={cookie}"}


def _title_from(run_input) -> str | None:
    for message in run_input.messages:
        if message.role == "user":
            for part in message.parts:
                if isinstance(part, TextUIPart) and part.text.strip():
                    return part.text.strip()[:_TITLE_LENGTH]
    return None


@post("/chat", status_code=200, penny_gated=True)
async def penny_chat(
    request: Request,
    current_credential: NamedDependency[Credential],
    current_ledger: NamedDependency[Ledger],
) -> Stream:
    """Streaming chat on the Vercel AI Data Stream protocol (SSE,
    sdk_version 6). The client sends the conversation id (client-minted
    UUIDv7) plus its **new** message only — the server's history is
    authoritative and is loaded, streamed against, and re-persisted whole
    on completion. Sessions chat with no extra ceremony; a PAT needs the
    ``penny`` scope. Inside the chat, tools run under the caller's real
    scopes regardless.
    """
    availability = chat_availability()
    if not availability.available:
        raise HTTPException(status_code=503, detail=f"Penny is unavailable: {availability.reason}")
    if not current_credential.penny:
        raise PermissionDeniedException(
            detail="This token does not permit Penny chat (missing 'penny' scope)"
        )

    run_input = VercelAIAdapter.build_run_input(await request.body())
    conversation_id = _conversation_uuid(run_input.id)

    row = await Conversation.where(lambda c: c.id == conversation_id).first()
    if row is not None and row.ledger_id != current_ledger.id:  # ty: ignore[unresolved-attribute]
        # The ownership 404 (never a cross-ledger append). A same-404 here
        # necessarily reveals the id exists somewhere — unavoidable in a
        # global id namespace, and unguessable at uuid scale.
        raise NotFoundException(detail="No such conversation")
    history = ModelMessagesTypeAdapter.validate_python(row.messages) if row else []

    ledger = current_ledger
    title = row.title if row else _title_from(run_input)

    adapter = VercelAIAdapter(
        agent=chat_agent,
        run_input=run_input,
        accept=request.headers.get("accept"),
        sdk_version=6,
    )
    deps = PennyDeps(app=request.app, auth_headers=_caller_headers(request))

    async def persist(result) -> None:
        """Runs inside the stream, inside the request's ferro session: the
        completed transcript becomes the conversation, whole."""
        messages = json.loads(result.all_messages_json())
        conversation = await Conversation.where(lambda c: c.id == conversation_id).first()
        if conversation is None:
            conversation = Conversation(id=conversation_id, ledger=ledger, title=title)
        conversation.messages = messages
        await conversation.save()
        log.info(
            "penny.chat.completed",
            conversation_id=str(conversation_id),
            ledger_id=str(ledger.id),
            messages=len(messages),
        )

    event_stream = adapter.build_event_stream()
    return Stream(
        adapter.encode_stream(
            adapter.run_stream(
                message_history=history,
                deps=deps,
                model=settings.ai_chat_model,
                on_complete=persist,
            )
        ),
        media_type=event_stream.content_type,
        headers=dict(event_stream.response_headers or {}),
    )


penny_router = Router(
    path="/api/v1/penny",
    tags=["penny"],
    route_handlers=[
        penny_status,
        penny_chat,
        list_conversations,
        get_conversation,
        delete_conversation,
    ],
)
