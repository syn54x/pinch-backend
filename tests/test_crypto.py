"""The provider-secret encryption utility (M7 CP1, issue #33): Fernet at
rest, keyed by settings. `Connection.encrypted_secret` waited since M1 for
this first consumer."""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from pinch_backend.crypto import decrypt_secret, encrypt_secret


@pytest.fixture
def keyed_settings(monkeypatch):
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "secret_encryption_key", Fernet.generate_key().decode())
    return settings


def test_round_trip(keyed_settings) -> None:
    blob = encrypt_secret("access-sandbox-abc123")
    assert isinstance(blob, bytes)
    assert b"access-sandbox-abc123" not in blob
    assert decrypt_secret(blob) == "access-sandbox-abc123"


def test_unkeyed_instance_refuses_loudly(monkeypatch) -> None:
    """Belt to the settings validator's suspenders: reaching encryption
    without a key is a programming error, not a silent no-op."""
    from pinch_backend.settings import settings

    monkeypatch.setattr(settings, "secret_encryption_key", "")
    with pytest.raises(RuntimeError, match="PINCH_SECRET_ENCRYPTION_KEY"):
        encrypt_secret("token")


def test_wrong_key_cannot_decrypt(keyed_settings, monkeypatch) -> None:
    blob = encrypt_secret("token")
    monkeypatch.setattr(keyed_settings, "secret_encryption_key", Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):  # the blob is bound to its key
        decrypt_secret(blob)
