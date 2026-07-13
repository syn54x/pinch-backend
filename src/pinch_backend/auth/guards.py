"""Request guards: resolve the acting credential, user, and ledger (PRD M2
story 13; PRD M3 story 3 adds the bearer path).

Registered as app-level dependencies so every router gets the chain by
declaring a parameter. It runs credential → user → ledger: the credential
is a session cookie *or* a bearer PAT, and everything downstream of
``current_user`` is credential-blind (issue #10: handlers cannot tell which
credential arrived). Cross-cutting policy lives at the choke points — the
hosted verification gate on the ledger, write-scope enforcement on the
credential — so no handler ever re-checks either.
"""

import uuid

# Runtime imports despite TC002: Litestar resolves dependency signatures at
# runtime, so these must be importable when annotations are evaluated.
from litestar import Request  # noqa: TC002
from litestar.di import NamedDependency  # noqa: TC002
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException
from pydantic.dataclasses import dataclass

from pinch_backend.auth.models import PatScope, PersonalAccessToken, Session
from pinch_backend.auth.pats import resolve_pat
from pinch_backend.auth.rate_limit import require_within_limit
from pinch_backend.auth.sessions import resolve_session
from pinch_backend.models import Ledger, LedgerMember, User
from pinch_backend.observability import get_logger
from pinch_backend.settings import settings

log = get_logger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class Credential:
    """The request's acting principal, whichever credential proved it.

    Only the guards themselves look inside: ``session`` is the
    credential-management fence (story 5), ``scope`` is write enforcement.
    Handlers consume ``current_user`` / ``current_ledger`` and stay blind.
    """

    user_id: uuid.UUID
    scope: PatScope
    """Sessions carry WRITE — a signed-in browser has the user's full
    power; scoping is what PATs add."""
    session: Session | None = None
    """Set iff the cookie authenticated the request."""
    pat: PersonalAccessToken | None = None
    """Set iff a bearer token authenticated the request."""


async def provide_current_credential(request: Request) -> Credential:
    """Resolve bearer-or-cookie into one credential (M3's single seam).

    Bearer wins when both are present and fails closed: with an
    ``Authorization: Bearer`` header, the cookie is never consulted, valid
    or not. That invariant is what makes the CSRF exemption for bearer
    requests sound (auth.csrf) — change them together or not at all.

    One 401 for every failure shape — no credential, unknown secret,
    expired session — so responses never say which part was wrong. Failed
    bearers are rate-limited per-IP (story 6); successes are not, so the
    limiter can never throttle legitimate API traffic.

    A read-scoped credential is refused on unsafe methods here (403),
    before any handler runs: write protection holds by construction for
    every endpoint M4+ ships (story 4).
    """
    authorization = request.headers.get("authorization", "")
    if authorization.split(" ", 1)[0].lower() == "bearer":
        _, _, secret = authorization.partition(" ")
        pat = await resolve_pat(secret.strip()) if secret.strip() else None
        if pat is None:
            ip = request.client.host if request.client else "unknown"
            await require_within_limit(
                f"bearer:ip:{ip}",
                limit=settings.auth_rate_limit_per_ip,
                window=settings.auth_rate_limit_window,
            )
            log.info("auth.bearer.failed", ip=ip)
            raise NotAuthorizedException(detail="Not authenticated")
        credential = Credential(
            user_id=pat.user_id,  # ty: ignore[unresolved-attribute]
            scope=pat.scope,
            pat=pat,
        )
    else:
        cookie_secret = request.cookies.get(settings.session_cookie_name)
        session = await resolve_session(cookie_secret) if cookie_secret else None
        if session is None:
            raise NotAuthorizedException(detail="Not authenticated")
        credential = Credential(
            user_id=session.user_id,  # ty: ignore[unresolved-attribute]
            scope=PatScope.WRITE,
            session=session,
        )
    if credential.scope is not PatScope.WRITE and request.method not in SAFE_METHODS:
        raise PermissionDeniedException(detail="This token does not permit write operations")
    return credential


async def provide_current_session(current_credential: NamedDependency[Credential]) -> Session:
    """The live *cookie* session behind the request — the credential-
    management fence (M3 story 5): endpoints that mint or revoke
    credentials declare this, so a leaked PAT can never escalate itself.

    The same 401 as every other auth failure: a 403 here would confirm to
    a stolen bearer that it is otherwise valid.
    """
    if current_credential.session is None:
        raise NotAuthorizedException(detail="Not authenticated")
    return current_credential.session


async def provide_current_user(current_credential: NamedDependency[Credential]) -> User:
    """The acting user — chains on the credential by parameter name."""
    return await User.get(current_credential.user_id)


async def provide_current_ledger(current_user: NamedDependency[User]) -> Ledger:
    """The user's active ledger — in v0, their single provisioned one.

    This is the gateway to domain data, so the hosted verification
    requirement lives here (PRD M2 story 10): auth endpoints stay reachable
    for an unverified user (they must be able to see /me and re-request
    the mail), the financial surface does not. It sits past the point where
    the credentials merge, so both pass the same gate (M3, issue #10).

    A user with no membership violates provision_user's atomicity
    invariant: loud 500, never a silent empty ledger (AGENTS I-1).
    """
    if settings.verification_required and current_user.email_verified_at is None:
        raise PermissionDeniedException(detail="Email verification required")
    membership = await LedgerMember.where(lambda m: m.user_id == current_user.id).first()
    if membership is None:
        raise RuntimeError(f"User {current_user.id} has no ledger membership")
    return await Ledger.get(membership.ledger_id)  # ty: ignore[unresolved-attribute]
