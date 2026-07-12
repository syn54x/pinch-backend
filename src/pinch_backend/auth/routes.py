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
from litestar import Request, Response, Router, get, post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException, NotAuthorizedException, PermissionDeniedException
from litestar.status_codes import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_409_CONFLICT
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from pinch_backend.auth import methods
from pinch_backend.auth.passwords import hash_password
from pinch_backend.auth.rate_limit import require_within_limit
from pinch_backend.auth.sessions import (
    clear_session_cookie,
    issue_session,
    resolve_session,
    session_cookie,
)
from pinch_backend.models import User, provision_user
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
    created_at: datetime


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        primary_currency=user.primary_currency,
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


auth_router = Router(path="/api/v1/auth", route_handlers=[signup, login, logout, me])
