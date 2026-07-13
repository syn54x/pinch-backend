"""M3 CP4 seam: the pinch CLI's first real commands (issue #11).

Real ``pinch`` invocations — argv parsing, config file, output — driven
against the real app through an in-process ASGI transport (the epic's
testing decision): only the CLI's HTTP client construction is swapped, so
everything the user types and sees is exercised, and no live network is
touched. ADR-0001 holds throughout: the CLI half of these tests knows only
URLs and JSON; the backend import below belongs to the test harness.

Tests are synchronous on purpose: each CLI command owns its event loop
(asyncio.run), exactly as in production.
"""

import asyncio
import io
import json
import stat
import sys

import pinch_cli.app as cli
import pytest
from litestar.testing import AsyncTestClient
from pinch_cli import config as cli_config

from pinch_backend.api.app import create_app
from pinch_backend.settings import settings

PASSWORD = "correct horse battery staple"


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A hermetic CLI home: throwaway config dir, file-backed database
    (each command's lifespan reconnects to it), no ambient env."""
    monkeypatch.setenv("PINCH_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("PINCH_SERVER_URL", raising=False)
    monkeypatch.delenv("PINCH_API_TOKEN", raising=False)
    monkeypatch.setattr(settings, "database_url", f"sqlite:{tmp_path / 'cli.db'}?mode=rwc")

    servers_seen: list[str] = []

    def asgi_client(server: str, token: str | None = None) -> AsyncTestClient:
        servers_seen.append(server)
        client = AsyncTestClient(create_app(), base_url="https://pinch.test")
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        return client

    monkeypatch.setattr(cli, "_client", asgi_client)
    return servers_seen


def _seed_pat() -> str:
    """Provision a user and mint a PAT over the public API; returns the secret."""

    async def seed() -> str:
        async with AsyncTestClient(create_app(), base_url="https://pinch.test") as c:
            await c.get("/health")
            csrf = {"x-csrftoken": c.cookies["csrftoken"]}
            signup = await c.post(
                "/api/v1/auth/signup",
                json={"email": "taylor@example.com", "password": PASSWORD},
                headers=csrf,
            )
            assert signup.status_code == 201
            minted = await c.post(
                "/api/v1/auth/pats",
                json={"name": "cli", "scopes": ["read", "write"]},
                headers={"x-csrftoken": c.cookies["csrftoken"]},
            )
            assert minted.status_code == 201
            return minted.json()["token"]

    return asyncio.run(seed())


def _run(tokens: list[str], expect: int = 0) -> None:
    """Invoke the CLI exactly as ``main()`` does; cyclopts sys.exits when
    the command returns, so the exit code is the observable."""
    with pytest.raises(SystemExit) as excinfo:
        cli.app(tokens)
    assert (excinfo.value.code or 0) == expect


def _login(monkeypatch, token: str, server: str = "https://pinch.test") -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    _run(["auth", "login", "--server", server])


# --- login stores; whoami proves; logout forgets (story 10) ---------------------


def test_login_stores_the_token_0600_and_whoami_prints_the_email(
    cli_env, monkeypatch, capsys
) -> None:
    token = _seed_pat()
    _login(monkeypatch, token)
    assert "taylor@example.com" in capsys.readouterr().out

    path = cli_config.config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert stored["token"] == token
    assert stored["server"] == "https://pinch.test"

    _run(["whoami"])
    assert "taylor@example.com" in capsys.readouterr().out


def test_logout_forgets_the_token_but_not_the_server(cli_env, monkeypatch, capsys) -> None:
    _login(monkeypatch, _seed_pat())
    _run(["auth", "logout"])

    stored = json.loads(cli_config.config_path().read_text())
    assert "token" not in stored
    assert stored["server"] == "https://pinch.test"

    capsys.readouterr()
    _run(["whoami"], expect=1)
    assert "pinch auth login" in capsys.readouterr().err


def test_a_rejected_login_stores_nothing_and_never_echoes_the_token(
    cli_env, monkeypatch, capsys
) -> None:
    _seed_pat()
    forged = "pinch_pat_" + "F" * 43
    monkeypatch.setattr(sys, "stdin", io.StringIO(forged + "\n"))

    _run(["auth", "login", "--server", "https://pinch.test"], expect=1)

    output = capsys.readouterr()
    assert forged not in output.out + output.err
    assert "rejected" in output.err
    assert "token" not in cli_config.load()


def test_a_revoked_token_gives_an_actionable_whoami_error(cli_env, monkeypatch, capsys) -> None:
    token = _seed_pat()
    _login(monkeypatch, token)

    async def revoke() -> None:
        from ferro import engines

        from pinch_backend.auth.models import PersonalAccessToken
        from pinch_backend.db import connect_database, disconnect_database

        await connect_database()
        async with engines.session():
            for pat in await PersonalAccessToken.all():
                await pat.delete()
        await disconnect_database()

    asyncio.run(revoke())

    capsys.readouterr()
    _run(["whoami"], expect=1)
    err = capsys.readouterr().err
    assert "rejected" in err
    assert token not in err


# --- Server selection: flag > env > config (story 11) ----------------------------


def test_server_resolution_prefers_flag_then_env_then_config(cli_env, monkeypatch, capsys) -> None:
    _login(monkeypatch, _seed_pat(), server="https://config.example")
    assert cli_env[-1] == "https://config.example"

    _run(["whoami"])  # nothing else set: the config file's server
    assert cli_env[-1] == "https://config.example"

    monkeypatch.setenv("PINCH_SERVER_URL", "https://env.example")
    _run(["whoami"])
    assert cli_env[-1] == "https://env.example"

    _run(["whoami", "--server", "https://flag.example"])
    assert cli_env[-1] == "https://flag.example"


def test_no_server_anywhere_is_a_clear_error(cli_env, capsys) -> None:
    _run(["whoami"], expect=1)
    err = capsys.readouterr().err
    assert "--server" in err
    assert "PINCH_SERVER_URL" in err
