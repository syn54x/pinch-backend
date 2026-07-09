"""The `pinch` command-line app.

Every command here is a thin wrapper over the Pinch developer API. This
package must never import `pinch_backend` — the HTTP boundary is the contract.
"""

import os

import httpx
from cyclopts import App

from pinch_cli import __version__

app = App(
    name="pinch",
    help="Pinch CLI — your finances, scriptable.",
    version=__version__,
    version_flags=["--version", "-V"],
)


def _client() -> httpx.Client:
    server_url = os.environ.get("PINCH_SERVER_URL", "http://localhost:8000")
    headers = {}
    if token := os.environ.get("PINCH_API_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=server_url, headers=headers, timeout=10.0)


@app.command
def health() -> None:
    """Check connectivity to the configured Pinch server."""
    with _client() as client:
        response = client.get("/health")
        response.raise_for_status()
        payload = response.json()
    print(f"server: {client.base_url}")
    print(f"status: {payload['status']} (v{payload['version']})")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
