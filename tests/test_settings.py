"""Settings that govern schema migration during active development (M5 CP1),
and the Plaid/encryption configuration contract (M7 CP1, issue #33)."""

import os
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

from pinch_backend.settings import Settings

# A name no real .env defines: asserting on a credential's name is how a
# credential's *value* ends up in failure output.
PROBE = "PINCH_TEST_DOTENV_PROBE"


def _probe_after_importing_settings(cwd, **env: str) -> str:
    """Import ``pinch_backend.settings`` in a fresh interpreter and report
    what the probe variable looks like afterwards.

    A subprocess, not ``importlib.reload``: reloading rebinds the module's
    ``settings`` object while every importer keeps the old one, so tests
    that monkeypatch settings would patch an instance the app never reads.
    An import-time side effect is only honestly observable from a process
    that has not imported the module yet."""
    child_env = {k: v for k, v in os.environ.items() if k != PROBE} | env
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import pinch_backend.settings, os; print(os.environ.get({PROBE!r}, ''))",
        ],
        cwd=cwd,
        env=child_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_importing_settings_puts_dotenv_on_the_process_environment(tmp_path) -> None:
    """Provider credentials are read from os.environ by pydantic-ai and
    logfire, never from Settings — pydantic-settings' own .env read fills
    that object and leaves the environment untouched, which is what left
    `uv run pinch-dev` without an API key while `just` recipes worked."""
    (tmp_path / ".env").write_text(f"{PROBE}=from-dotenv\n")

    assert _probe_after_importing_settings(tmp_path) == "from-dotenv"


def test_the_dotenv_read_is_relative_to_the_working_directory(tmp_path) -> None:
    """Pins the explicit path argument: bare `load_dotenv()` walks up from
    the calling module's directory, so it would read the repo's own .env
    from anywhere — and disagree with `env_file`, which is cwd-relative."""
    assert _probe_after_importing_settings(tmp_path) == ""  # no .env here


def test_a_real_environment_variable_beats_the_dotenv_file(tmp_path) -> None:
    """Same precedence pydantic-settings uses: the environment wins, so an
    inline `FOO=bar uv run ...` override still means what it says."""
    (tmp_path / ".env").write_text(f"{PROBE}=from-dotenv\n")

    assert _probe_after_importing_settings(tmp_path, **{PROBE: "from-shell"}) == "from-shell"


def test_migration_flags_default_on_for_development() -> None:
    s = Settings()
    assert s.database_migrate_updates is True
    assert s.database_migrate_destructive is True


def test_plaid_unconfigured_by_default() -> None:
    s = Settings()
    assert s.plaid_configured is False


def test_plaid_configured_requires_encryption_key() -> None:
    """A half-configured instance fails at startup, not at first link
    (PRD #31: loud startup failure)."""
    with pytest.raises(ValueError, match="PINCH_SECRET_ENCRYPTION_KEY"):
        Settings(plaid_client_id="cid", plaid_secret="sec")


def test_plaid_configured_requires_webhook_url() -> None:
    """Webhooks are required, not optional (ADR 0008): a Plaid instance
    that can't be rung must fail at startup, never silently degrade to
    never-syncing."""
    with pytest.raises(ValueError, match="PINCH_PLAID_WEBHOOK_URL"):
        Settings(
            plaid_client_id="cid",
            plaid_secret="sec",
            secret_encryption_key=Fernet.generate_key().decode(),
        )


def test_keyless_startup_needs_no_webhook_url() -> None:
    """A keyless instance (no Plaid) is untouched by the webhook
    requirement — manual tracking never held hostage."""
    s = Settings()
    assert s.plaid_configured is False
    assert s.plaid_webhook_url == ""


def test_plaid_configured_with_key() -> None:
    s = Settings(
        plaid_client_id="cid",
        plaid_secret="sec",
        secret_encryption_key=Fernet.generate_key().decode(),
        plaid_webhook_url="https://pinch.example/webhooks/plaid",
    )
    assert s.plaid_configured is True
    assert s.plaid_environment == "sandbox"


def test_mx_unconfigured_by_default() -> None:
    s = Settings()
    assert s.mx_configured is False
    assert s.mx_environment == "sandbox"


def test_mx_configured_needs_both_credentials() -> None:
    """The pair is the credential (ADR 0009): half a pair is unconfigured,
    never half-working."""
    assert Settings(mx_client_id="cid").mx_configured is False
    assert Settings(mx_api_key="key").mx_configured is False
    assert Settings(mx_client_id="cid", mx_api_key="key").mx_configured is True


def test_mx_needs_no_encryption_key() -> None:
    """An MX-only instance skips Plaid-only machinery (PRD #86 story 17):
    MX mints no per-connection secret, so the encryption-key requirement
    stays Plaid's. No webhook-secret requirement yet either — that
    validator lands with the receiver (CP4, #91)."""
    s = Settings(mx_client_id="cid", mx_api_key="key")
    assert s.mx_configured is True
    assert s.secret_encryption_key == ""
    assert s.mx_webhook_secret == ""


def test_reconcile_interval_defaults_dev_friendly() -> None:
    """24h default so dev machines aren't ticking hourly for no one
    (PRD #77 story 17); production sets PINCH_RECONCILE_INTERVAL_HOURS=1."""
    assert Settings().reconcile_interval_hours == 24


def test_ai_model_knobs_unset_by_default() -> None:
    """One knob per agent, holding a pydantic-ai model string (PRD M9).
    Empty means that agent is disabled — keyless is a first-class state,
    and the conftest blanks the developer's .env values so the suite
    always tests that baseline."""
    s = Settings()
    assert s.ai_chat_model == ""
    assert s.ai_categorization_model == ""
    assert s.ai_mapping_model == ""
