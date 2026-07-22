"""/api/v1/auth — the platform's first public routes (PRD M2; conventions
here bind M3).

Every handler observes the secrets discipline: passwords arrive as
``SecretStr``, leave as argon2id hashes, and responses are built from an
explicit allowlist model — a leak requires adding a field, not forgetting
to remove one.
"""

import uuid
from datetime import datetime

from ferro import UniqueViolationError
from litestar import Request, Response, Router, delete, get, patch, post
from litestar.di import NamedDependency
from litestar.exceptions import (
    HTTPException,
    NotAuthorizedException,
    NotFoundException,
    PermissionDeniedException,
)
from litestar.params import FromPath
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_202_ACCEPTED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_409_CONFLICT,
)
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from pinch_backend.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    CursorParam,
    LimitParam,
    Page,
    paginate,
)
from pinch_backend.auth import flows, methods
from pinch_backend.auth.breach import password_is_breached
from pinch_backend.auth.models import PatScope, PersonalAccessToken, Session
from pinch_backend.auth.passwords import hash_password
from pinch_backend.auth.pats import issue_pat
from pinch_backend.auth.rate_limit import require_within_limit
from pinch_backend.auth.sessions import (
    clear_session_cookie,
    issue_session,
    resolve_session,
    session_cookie,
)
from pinch_backend.models import User, provision_user, utcnow
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)


class SignupRequest(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    email: EmailStr
    password: SecretStr = Field(min_length=8)
    """Breach corpus checking (HIBP) is the real gate and lands in CP4;
    a length floor is the NIST-baseline backstop, not the defense."""
    display_name: str | None = None
    """Defaults to the email's local part."""
    primary_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    """Chosen at signup (CONTEXT.md: Money)."""


class LoginRequest(BaseModel):
    email: str
    """Deliberately not EmailStr: a malformed email must fail exactly like a
    wrong password (401 after the method runs), never a distinguishable 400."""
    password: SecretStr


class UserOut(BaseModel):
    """What a client may see about itself — an allowlist, never the row."""

    id: uuid.UUID
    email: str
    display_name: str
    primary_currency: str
    email_verified: bool
    created_at: datetime


class MePatchIn(BaseModel):
    """The self-profile allowlist (F3 enabler #42): what a signed-in user may
    change about themselves after signup. Unknown keys are a 400
    (extra="forbid"), never a silently accepted no-op — the transactions
    PATCH convention."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="forbid")

    primary_currency: str = Field(pattern=r"^[A-Z]{3}$")
    """ISO 4217 alpha code, same shape rule as signup — unrestricted in v0
    (reports re-express at current rates), so any three uppercase letters
    pass and anything else is a 400."""


class SessionOut(BaseModel):
    """One row of "where am I signed in?" (PRD story 6). No token material
    in any form — the id is the revocation handle."""

    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    client_hint: str | None
    current: bool


class PatCreateIn(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)

    name: str = Field(min_length=1, max_length=100)
    """User-chosen label; display-only, never unique."""
    scopes: list[PatScope] = Field(min_length=1)
    """The requested grant. Write implies read (PRD M3): the stored scope
    is the strongest one requested, and responses render the full grant."""


class PatOut(BaseModel):
    """One row of the PAT list (story 2) — an allowlist, never the row.
    No secret material beyond the display prefix."""

    id: uuid.UUID
    name: str
    scopes: list[PatScope]
    display_prefix: str
    created_at: datetime
    last_used_at: datetime | None


class PatCreatedOut(PatOut):
    """The create response: the only place the secret ever appears (story 1)."""

    token: str


class TokenIn(BaseModel):
    token: SecretStr


class PasswordResetRequestIn(BaseModel):
    email: str
    """Plain str, like login: an unknown or malformed email must answer
    exactly like a known one (202)."""


class PasswordResetConfirmIn(BaseModel):
    token: SecretStr
    password: SecretStr = Field(min_length=8)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        primary_currency=user.primary_currency,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@post("/signup")
async def signup(data: SignupRequest, request: Request) -> Response[UserOut]:
    if not settings.signup_enabled:
        raise PermissionDeniedException(detail="Signup is disabled on this instance")
    await require_within_limit(
        f"signup:ip:{_client_ip(request)}",
        limit=settings.auth_rate_limit_per_ip,
        window=settings.auth_rate_limit_window,
    )
    if await password_is_breached(data.password.get_secret_value()):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="This password appears in known data breaches; choose a different one",
        )
    try:
        user = await provision_user(
            email=data.email,
            display_name=data.display_name or data.email.split("@")[0],
            primary_currency=data.primary_currency,
            password_hash=hash_password(data.password.get_secret_value()),
        )
    except UniqueViolationError:
        log.info("auth.signup.duplicate", email=data.email.lower())
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from None
    session, secret = await issue_session(user, client_hint=request.headers.get("user-agent"))
    await flows.start_email_verification(user)
    log.info("auth.signup", user_id=str(user.id), session_id=str(session.id))
    return Response(_user_out(user), cookies=[session_cookie(secret)])


@post("/login", status_code=HTTP_200_OK)
async def login(data: LoginRequest, request: Request) -> Response[UserOut]:
    email = data.email.strip().lower()
    await require_within_limit(
        f"login:ip:{_client_ip(request)}",
        limit=settings.auth_rate_limit_per_ip,
        window=settings.auth_rate_limit_window,
    )
    await require_within_limit(
        f"login:email:{email}",
        limit=settings.auth_rate_limit_per_email,
        window=settings.auth_rate_limit_window,
    )
    user = await methods.get("password").authenticate({"email": email, "password": data.password})
    if user is None:
        log.info("auth.login.failed", email=email, method="password")
        raise NotAuthorizedException(detail="Invalid credentials")
    session, secret = await issue_session(user, client_hint=request.headers.get("user-agent"))
    log.info("auth.login", user_id=str(user.id), session_id=str(session.id), method="password")
    return Response(_user_out(user), cookies=[session_cookie(secret)])


@post("/logout", status_code=HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response[None]:
    """Idempotent: the outcome "you are signed out" holds with or without a
    live session, so the answer is 204 either way."""
    secret = request.cookies.get(settings.session_cookie_name)
    if secret:
        session = await resolve_session(secret)
        if session is not None:
            await session.delete()
            log.info(
                "auth.logout",
                user_id=str(session.user_id),  # ty: ignore[unresolved-attribute]
                session_id=str(session.id),
            )
    return Response(None, cookies=[clear_session_cookie()])


@get("/me")
async def me(current_user: NamedDependency[User]) -> UserOut:
    return _user_out(current_user)


@patch("/me")
async def update_me(data: MePatchIn, current_user: NamedDependency[User]) -> UserOut:
    """Onboarding's currency step (F3 enabler #42): the value pre-fills from
    ``me`` and this writes it back. Credential-blind like ``me`` itself — a
    write-scoped PAT may update the profile; only credential management is
    cookie-fenced."""
    current_user.primary_currency = data.primary_currency
    await current_user.save()
    log.info(
        "auth.me.updated",
        user_id=str(current_user.id),
        primary_currency=data.primary_currency,
    )
    return _user_out(current_user)


@get("/sessions")
async def list_sessions(
    current_session: NamedDependency[Session],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[SessionOut]:
    """The first consumer of the M3 pagination convention (issue #9).

    Liveness is filtered in SQL — the same checks as ``Session.is_active`` —
    so pages stay full; filtering after the query would let dead rows eat
    page slots.
    """
    now = utcnow()
    idle_cutoff = now - settings.session_idle_ttl
    user_id = current_session.user_id  # ty: ignore[unresolved-attribute]
    query = Session.where(
        lambda s: (
            (s.user_id == user_id) & (s.absolute_expires_at > now) & (s.last_seen_at > idle_cutoff)
        )
    )
    rows, next_cursor = await paginate(query, cursor=cursor, limit=limit)
    return Page(
        items=[
            SessionOut(
                id=s.id,
                created_at=s.created_at,
                last_seen_at=s.last_seen_at,
                client_hint=s.client_hint,
                current=s.id == current_session.id,
            )
            for s in rows
        ],
        next_cursor=next_cursor,
    )


@delete("/sessions/{session_id:uuid}")
async def revoke_session(
    session_id: FromPath[uuid.UUID], current_session: NamedDependency[Session]
) -> None:
    user_id = current_session.user_id  # ty: ignore[unresolved-attribute]
    row = await Session.where(lambda s: (s.id == session_id) & (s.user_id == user_id)).first()
    if row is None:
        # 404 for someone else's session too — never confirm it exists.
        raise NotFoundException(detail="No such session")
    await row.delete()
    log.info("auth.session.revoked", user_id=str(user_id), session_id=str(session_id))


def _scopes_out(scope: PatScope) -> list[PatScope]:
    """Render the stored rank as the full grant: write implies read, and
    the wire format stays a list so per-resource scopes later are additive."""
    return [PatScope.READ, PatScope.WRITE] if scope is PatScope.WRITE else [PatScope.READ]


@post("/pats")
async def create_pat(data: PatCreateIn, current_session: NamedDependency[Session]) -> PatCreatedOut:
    """Cookie-session only, via ``current_session`` (story 5): a PAT can
    never mint PATs."""
    user = await User.get(current_session.user_id)  # ty: ignore[unresolved-attribute]
    # Minting is bounded per user: a hostile or runaway session can't spam
    # unbounded credential rows. Same per-principal knob as the other
    # credentialed endpoints.
    await require_within_limit(
        f"pat-create:user:{user.id}",
        limit=settings.auth_rate_limit_per_email,
        window=settings.auth_rate_limit_window,
    )
    scope = PatScope.WRITE if PatScope.WRITE in data.scopes else PatScope.READ
    pat, secret = await issue_pat(user, name=data.name, scope=scope)
    log.info("auth.pat.created", user_id=str(user.id), pat_id=str(pat.id), scope=scope.value)
    return PatCreatedOut(
        id=pat.id,
        name=pat.name,
        scopes=_scopes_out(pat.scope),
        display_prefix=pat.display_prefix,
        created_at=pat.created_at,
        last_used_at=pat.last_used_at,
        token=secret,
    )


@get("/pats")
async def list_pats(
    current_session: NamedDependency[Session],
    cursor: CursorParam = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
) -> Page[PatOut]:
    """Born onto the pagination convention (issue #9), and behind the same
    cookie fence as the rest of PAT management."""
    user_id = current_session.user_id  # ty: ignore[unresolved-attribute]
    rows, next_cursor = await paginate(
        PersonalAccessToken.where(lambda p: p.user_id == user_id), cursor=cursor, limit=limit
    )
    return Page(
        items=[
            PatOut(
                id=p.id,
                name=p.name,
                scopes=_scopes_out(p.scope),
                display_prefix=p.display_prefix,
                created_at=p.created_at,
                last_used_at=p.last_used_at,
            )
            for p in rows
        ],
        next_cursor=next_cursor,
    )


@delete("/pats/{pat_id:uuid}")
async def revoke_pat(
    pat_id: FromPath[uuid.UUID], current_session: NamedDependency[Session]
) -> None:
    """Revocation is row deletion (dead = gone, like sessions); the audit
    trail is the structured event."""
    user_id = current_session.user_id  # ty: ignore[unresolved-attribute]
    row = await PersonalAccessToken.where(
        lambda p: (p.id == pat_id) & (p.user_id == user_id)
    ).first()
    if row is None:
        # 404 for someone else's PAT too — never confirm it exists.
        raise NotFoundException(detail="No such token")
    await row.delete()
    log.info("auth.pat.revoked", user_id=str(user_id), pat_id=str(pat_id))


@post("/email-verification/request", status_code=HTTP_202_ACCEPTED)
async def request_email_verification(current_user: NamedDependency[User]) -> None:
    await require_within_limit(
        f"verify:email:{current_user.email}",
        limit=settings.auth_rate_limit_per_email,
        window=settings.auth_rate_limit_window,
    )
    await flows.start_email_verification(current_user)


@post("/email-verification/confirm", status_code=HTTP_204_NO_CONTENT)
async def confirm_email_verification(data: TokenIn, request: Request) -> None:
    await require_within_limit(
        f"verify-confirm:ip:{_client_ip(request)}",
        limit=settings.auth_rate_limit_per_ip,
        window=settings.auth_rate_limit_window,
    )
    if not await flows.confirm_email_verification(data.token.get_secret_value()):
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Invalid or expired token")


@post("/password-reset/request", status_code=HTTP_202_ACCEPTED)
async def request_password_reset(data: PasswordResetRequestIn, request: Request) -> None:
    """202 whether or not the email exists — the mailbox is the only place
    the difference shows."""
    email = data.email.strip().lower()
    await require_within_limit(
        f"reset:ip:{_client_ip(request)}",
        limit=settings.auth_rate_limit_per_ip,
        window=settings.auth_rate_limit_window,
    )
    await require_within_limit(
        f"reset:email:{email}",
        limit=settings.auth_rate_limit_per_email,
        window=settings.auth_rate_limit_window,
    )
    await flows.start_password_reset(email)


@post("/password-reset/confirm", status_code=HTTP_204_NO_CONTENT)
async def confirm_password_reset(data: PasswordResetConfirmIn, request: Request) -> None:
    await require_within_limit(
        f"reset-confirm:ip:{_client_ip(request)}",
        limit=settings.auth_rate_limit_per_ip,
        window=settings.auth_rate_limit_window,
    )
    new_password = data.password.get_secret_value()
    if await password_is_breached(new_password):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="This password appears in known data breaches; choose a different one",
        )
    if not await flows.complete_password_reset(data.token.get_secret_value(), new_password):
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Invalid or expired token")


auth_router = Router(
    path="/api/v1/auth",
    route_handlers=[
        signup,
        login,
        logout,
        me,
        update_me,
        list_sessions,
        revoke_session,
        create_pat,
        list_pats,
        revoke_pat,
        request_email_verification,
        confirm_email_verification,
        request_password_reset,
        confirm_password_reset,
    ],
)
