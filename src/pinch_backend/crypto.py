"""Encryption at rest for provider secrets (M7 CP1, issue #33).

Fernet (AEAD, versioned tokens), keyed by ``settings.secret_encryption_key``.
v0 is single-key; rotation is the documented ``MultiFernet`` upgrade path —
decrypt with old keys, encrypt with the new — no machinery until needed.

The plaintext (a Plaid access token) is write-only at the API surface: it
is encrypted before the row is saved and never appears in a response, a log
line, or ``error_detail``.
"""

from cryptography.fernet import Fernet

from pinch_backend.settings import settings


def _fernet() -> Fernet:
    if not settings.secret_encryption_key:
        # The settings validator makes this unreachable on a booted app;
        # loud beats silent if a future caller sidesteps it.
        raise RuntimeError("PINCH_SECRET_ENCRYPTION_KEY is not configured")
    return Fernet(settings.secret_encryption_key.encode())


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(blob: bytes) -> str:
    return _fernet().decrypt(blob).decode()
