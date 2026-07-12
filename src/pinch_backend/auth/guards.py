"""Request guards: resolve the acting user and their ledger (PRD M2 story 13).

Registered as app-level dependencies so every later router gets
``current_user`` / ``current_ledger`` by declaring the parameter —
authorization one dependency away, everywhere (M3 consumes this).
"""

# Runtime imports despite TC002: Litestar resolves dependency signatures at
# runtime, so these must be importable when annotations are evaluated.
from litestar import Request  # noqa: TC002
from litestar.di import NamedDependency  # noqa: TC002
from litestar.exceptions import NotAuthorizedException

from pinch_backend.auth.sessions import resolve_session
from pinch_backend.models import Ledger, LedgerMember, User
from pinch_backend.settings import settings


async def provide_current_user(request: Request) -> User:
    """The acting user, resolved from the session cookie.

    One 401 for every failure shape — no cookie, unknown secret, expired
    session — so responses never say which part was wrong.
    """
    secret = request.cookies.get(settings.session_cookie_name)
    if secret:
        session = await resolve_session(secret)
        if session is not None:
            # Shadow *_id columns are runtime-synthesized; invisible to ty.
            return await User.get(session.user_id)  # ty: ignore[unresolved-attribute]
    raise NotAuthorizedException(detail="Not authenticated")


async def provide_current_ledger(current_user: NamedDependency[User]) -> Ledger:
    """The user's active ledger — in v0, their single provisioned one.

    Chains on ``current_user`` by parameter name (Litestar DI). A user with
    no membership violates provision_user's atomicity invariant: loud 500,
    never a silent empty ledger (AGENTS I-1).
    """
    membership = await LedgerMember.where(lambda m: m.user_id == current_user.id).first()
    if membership is None:
        raise RuntimeError(f"User {current_user.id} has no ledger membership")
    return await Ledger.get(membership.ledger_id)  # ty: ignore[unresolved-attribute]
