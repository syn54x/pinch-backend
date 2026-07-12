"""Opaque bearer tokens: 256-bit secrets, hashed at rest (PRD M2).

The secret goes to the client exactly once (cookie or mailed link); only
its SHA-256 hex digest is stored. A leaked database therefore leaks no
usable credentials. SHA-256 — not argon2 — because these secrets are
256 bits of ``secrets``-module randomness: unguessable by construction,
so slow hashing buys nothing and a plain digest keeps lookup an indexed
equality query.
"""

import hashlib
import secrets

from pydantic.dataclasses import dataclass


@dataclass(frozen=True)
class IssuedToken:
    """Named fields so secret and hash can't be swapped at a call site."""

    secret: str
    """Hand to the client once; never store, log, or echo it."""
    token_hash: str
    """The only form that touches the database."""


def generate_token() -> IssuedToken:
    secret = secrets.token_urlsafe(32)
    return IssuedToken(secret=secret, token_hash=hash_token(secret))


def hash_token(secret: str) -> str:
    """Digest a presented secret for lookup against stored hashes."""
    return hashlib.sha256(secret.encode()).hexdigest()
