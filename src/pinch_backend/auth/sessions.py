"""Session issuance and resolution — where every login method terminates.

The secret leaves this module exactly once, inside the cookie returned by
``session_cookie``; the database only ever sees its hash. A session that
fails either expiry check is deleted on resolution: dead means gone, and
the session list never shows corpses.
"""

from datetime import timedelta

from litestar.datastructures import Cookie

from pinch_backend.auth.models import Session
from pinch_backend.auth.tokens import generate_token, hash_token
from pinch_backend.models import User, utcnow
from pinch_backend.settings import settings

TOUCH_GRANULARITY = timedelta(minutes=1)
"""Floor between last-seen writes, so a busy dashboard doesn't turn every
request into an UPDATE. Idle TTLs are days; minute-grade is exact enough."""

_CLIENT_HINT_MAX = 256


async def issue_session(user: User, *, client_hint: str | None = None) -> tuple[Session, str]:
    """The single termination point of every login method (ADR-0005).

    Returns the session row and the one-time secret for the cookie.
    """
    token = generate_token()
    session = await Session.create(
        user=user,
        token_hash=token.token_hash,
        client_hint=client_hint[:_CLIENT_HINT_MAX] if client_hint else None,
        absolute_expires_at=utcnow() + settings.session_absolute_ttl,
    )
    return session, token.secret


async def resolve_session(secret: str) -> Session | None:
    """Return the live session for a presented secret, or None.

    Expired sessions are deleted here rather than by a sweeper: resolution
    is the only moment staleness matters, and lazy deletion keeps "it
    exists" equivalent to "it works".
    """
    session = await Session.where(lambda s: s.token_hash == hash_token(secret)).first()
    if session is None:
        return None
    now = utcnow()
    if not session.is_active(idle_ttl=settings.session_idle_ttl, now=now):
        await session.delete()
        return None
    if now - session.last_seen_at >= TOUCH_GRANULARITY:
        session.last_seen_at = now
        await session.save()
    return session


def session_cookie(secret: str) -> Cookie:
    """httpOnly + Secure + SameSite=Lax, bounded by the absolute TTL."""
    return Cookie(
        key=settings.session_cookie_name,
        value=secret,
        max_age=int(settings.session_absolute_ttl.total_seconds()),
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


def clear_session_cookie() -> Cookie:
    return Cookie(
        key=settings.session_cookie_name,
        value="",
        max_age=0,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
