"""Request guards: resolve the acting session, user, and ledger (PRD M2
story 13).

Registered as app-level dependencies so every later router gets
``current_session`` / ``current_user`` / ``current_ledger`` by declaring
the parameter — authorization one dependency away, everywhere (M3
consumes this). They chain: session → user → ledger.
"""

# Runtime imports despite TC002: Litestar resolves dependency signatures at
# runtime, so these must be importable when annotations are evaluated.
from litestar import Request  # noqa: TC002
from litestar.di import NamedDependency  # noqa: TC002
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException

from pinch_backend.auth.models import Session  # noqa: TC001 — runtime, Litestar DI
from pinch_backend.auth.sessions import resolve_session
from pinch_backend.models import Ledger, LedgerMember, User
from pinch_backend.settings import settings


async def provide_current_session(request: Request) -> Session:
    """The live session behind the request's cookie.

    One 401 for every failure shape — no cookie, unknown secret, expired
    session — so responses never say which part was wrong.
    """
    secret = request.cookies.get(settings.session_cookie_name)
    if secret:
        session = await resolve_session(secret)
        if session is not None:
            return session
    raise NotAuthorizedException(detail="Not authenticated")


async def provide_current_user(current_session: NamedDependency[Session]) -> User:
    """The acting user — chains on the session by parameter name."""
    # Shadow *_id columns are runtime-synthesized; invisible to ty.
    return await User.get(current_session.user_id)  # ty: ignore[unresolved-attribute]


async def provide_current_ledger(current_user: NamedDependency[User]) -> Ledger:
    """The user's active ledger — in v0, their single provisioned one.

    This is the gateway to domain data, so the hosted verification
    requirement lives here (PRD story 10): auth endpoints stay reachable
    for an unverified user (they must be able to see /me and re-request
    the mail), the financial surface does not.

    A user with no membership violates provision_user's atomicity
    invariant: loud 500, never a silent empty ledger (AGENTS I-1).
    """
    if settings.verification_required and current_user.email_verified_at is None:
        raise PermissionDeniedException(detail="Email verification required")
    membership = await LedgerMember.where(lambda m: m.user_id == current_user.id).first()
    if membership is None:
        raise RuntimeError(f"User {current_user.id} has no ledger membership")
    return await Ledger.get(membership.ledger_id)  # ty: ignore[unresolved-attribute]
