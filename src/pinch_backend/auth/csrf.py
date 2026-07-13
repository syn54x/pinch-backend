"""Credential-aware CSRF (PRD M3, issue #10): cookie requests keep M2's
double-submit check; bearer requests are exempt *by construction*.

A cross-site form or fetch cannot attach an ``Authorization`` header, so a
request presenting one cannot be a CSRF. The exemption is sound only
because the credential resolver enforces bearer-wins-and-fails-closed
(``guards.provide_current_credential``): when a bearer header is present,
the session cookie is never the acting credential — so skipping the check
here can never let a cookie act unprotected. These two halves are one
design; change them together or not at all.
"""

from typing import TYPE_CHECKING

from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.middleware.csrf import CSRFMiddleware

if TYPE_CHECKING:
    from litestar.types import Receive, Scope, Send


class CredentialAwareCSRFMiddleware(CSRFMiddleware):
    """Litestar's stock CSRF middleware, skipped for bearer-schemed requests."""

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] == ScopeType.HTTP:
            authorization = Headers.from_scope(scope).get("authorization", "")
            if authorization.split(" ", 1)[0].lower() == "bearer":
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)
