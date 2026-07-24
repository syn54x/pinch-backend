"""The capability pattern's plumbing (PRD M9, CP0-proven): every tool call
is an in-process HTTP request to the public v1 API — httpx over ASGI
transport, no network hop — authenticated by forwarding the chatting
caller's own credential headers. A PAT-scoped caller yields a Penny with
exactly those scopes; parity can't drift because there is no other door.
"""

from dataclasses import dataclass
from typing import Any

import httpx

_SELF_CALL_BASE = "https://penny.self-call.internal"
"""Never resolved: the ASGI transport routes by path; the host exists only
because httpx requires an absolute base URL."""


class ApiDeclined(Exception):
    """A non-2xx answer from the public API, carrying the sentence the
    model relays — tool-level denials are reported conversationally,
    never hidden (PRD M9)."""


@dataclass
class PennyDeps:
    """Per-request run dependencies for Penny's agents."""

    app: Any
    """The running Litestar app (any ASGI callable): the self-call target,
    taken from the live request so tests and production agree for free."""
    auth_headers: dict[str, str]
    """The caller's own credential, verbatim — ``Authorization`` for a
    bearer, ``Cookie`` for a session. Read tools are safe methods, so no
    CSRF material is needed or forwarded."""


async def api_get(deps: PennyDeps, path: str, params: dict[str, Any] | None = None) -> Any:
    """One in-process GET as the caller; parsed JSON on 2xx.

    Non-2xx raises ApiDeclined with the error envelope's ``detail`` — the
    API's own words, which are stable enough to display and therefore
    stable enough for Penny to relay.
    """
    transport = httpx.ASGITransport(app=deps.app)
    async with httpx.AsyncClient(
        transport=transport, base_url=_SELF_CALL_BASE, headers=deps.auth_headers
    ) as client:
        filtered = {k: v for k, v in (params or {}).items() if v is not None}
        response = await client.get(path, params=filtered)
    if response.is_success:
        return response.json()
    try:
        detail = response.json().get("detail", response.reason_phrase)
    except ValueError:
        detail = response.reason_phrase
    raise ApiDeclined(f"The API declined this request: {detail} (HTTP {response.status_code})")
