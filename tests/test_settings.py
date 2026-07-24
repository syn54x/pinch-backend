"""Settings that govern schema migration during active development (M5 CP1),
and the Plaid/encryption configuration contract (M7 CP1, issue #33)."""

import pytest
from cryptography.fernet import Fernet

from pinch_backend.settings import Settings


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


def test_plaid_configured_with_key() -> None:
    s = Settings(
        plaid_client_id="cid",
        plaid_secret="sec",
        secret_encryption_key=Fernet.generate_key().decode(),
    )
    assert s.plaid_configured is True
    assert s.plaid_environment == "sandbox"


def test_ai_model_knobs_unset_by_default() -> None:
    """One knob per agent, holding a pydantic-ai model string (PRD M9).
    Empty means that agent is disabled — keyless is a first-class state,
    and the conftest blanks the developer's .env values so the suite
    always tests that baseline."""
    s = Settings()
    assert s.ai_chat_model == ""
    assert s.ai_categorization_model == ""
    assert s.ai_mapping_model == ""
