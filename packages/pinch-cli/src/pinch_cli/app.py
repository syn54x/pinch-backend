"""The `pinch` command-line app.

Every command here is a thin wrapper over the Pinch developer API. This
package must never import `pinch_backend` — the HTTP boundary is the contract
(ADR-0001).

Commands own their event loop: each runs asyncio.run over an httpx
AsyncClient from ``_client``, which is also the seam the test suite swaps
for an in-process ASGI transport.
"""

import asyncio
import getpass
import os
import sys

import httpx
from cyclopts import App

from pinch_cli import __version__, config

ME = "/api/v1/auth/me"

app = App(
    name="pinch",
    help="Pinch CLI — your finances, scriptable.",
    version=__version__,
    version_flags=["--version", "-V"],
)

auth_app = App(name="auth", help="Log in to a Pinch server and manage the stored token.")
app.command(auth_app)


def _client(server: str, token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(base_url=server, headers=headers, timeout=10.0)


def _fail(message: str) -> "SystemExit":
    print(message, file=sys.stderr)
    return SystemExit(1)


def _resolve_server(flag: str | None) -> str:
    """--server flag > PINCH_SERVER_URL > config file (issue #11)."""
    server = flag or os.environ.get("PINCH_SERVER_URL") or config.load().get("server")
    if not server:
        raise _fail(
            "No server configured. Pass --server, set PINCH_SERVER_URL, "
            "or run 'pinch auth login --server <url>'."
        )
    return server


def _resolve_token() -> str | None:
    return os.environ.get("PINCH_API_TOKEN") or config.load().get("token")


def _read_token() -> str:
    """Prompt without echo on a TTY; read piped stdin otherwise. The token
    never transits argv, so it can't leak into shell history or ps."""
    if sys.stdin.isatty():
        token = getpass.getpass("Paste your personal access token: ")
    else:
        token = sys.stdin.readline()
    token = token.strip()
    if not token:
        raise _fail("No token provided.")
    return token


@auth_app.command
def login(*, server: str | None = None) -> None:
    """Store a personal access token after proving it works.

    Reads the token from a no-echo prompt (or stdin when piped), verifies
    it against the server, and only then writes it to the config file.
    """
    resolved = _resolve_server(server)
    token = _read_token()

    async def verify() -> dict:
        async with _client(resolved, token) as client:
            response = await client.get(ME)
        if response.status_code == 401:
            raise _fail(
                f"The server at {resolved} rejected the token. Check that it has not been revoked."
            )
        response.raise_for_status()
        return response.json()

    user = asyncio.run(verify())
    config.save(config.load() | {"server": resolved, "token": token})
    print(f"Logged in to {resolved} as {user['email']}")


@auth_app.command
def logout() -> None:
    """Forget the stored token.

    Local only: the token itself stays valid until revoked in the app —
    a token can never revoke tokens (that requires a browser session).
    """
    stored = config.load()
    stored.pop("token", None)
    config.save(stored)
    print("Logged out.")


@app.command
def whoami(*, server: str | None = None) -> None:
    """Print the identity behind the stored (or PINCH_API_TOKEN) credential."""
    resolved = _resolve_server(server)
    token = _resolve_token()
    if not token:
        raise _fail("Not logged in. Run 'pinch auth login', or set PINCH_API_TOKEN.")

    async def me() -> dict:
        async with _client(resolved, token) as client:
            response = await client.get(ME)
        if response.status_code == 401:
            raise _fail(
                f"The server at {resolved} rejected your credentials. "
                "Run 'pinch auth login' to refresh them."
            )
        response.raise_for_status()
        return response.json()

    print(asyncio.run(me())["email"])


@app.command
def health(*, server: str | None = None) -> None:
    """Check connectivity to the configured Pinch server."""
    resolved = _resolve_server(server)

    async def check() -> dict:
        async with _client(resolved) as client:
            response = await client.get("/health")
        response.raise_for_status()
        return response.json()

    payload = asyncio.run(check())
    print(f"server: {resolved}")
    print(f"status: {payload['status']} (v{payload['version']})")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
