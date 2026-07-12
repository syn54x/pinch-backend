"""Password hashing: argon2id with pinned parameters (PRD M2, ADR-0005).

Parameters are pinned here — not inherited from argon2-cffi defaults — so a
library upgrade can never change them silently. The values are RFC 9106's
second recommended option (64 MiB memory, 3 passes, 4 lanes), the standard
choice for interactive logins. Raising them later is safe: ``needs_rehash``
flags old hashes and login re-hashes on the next successful verify.
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # KiB — 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """True only for a genuine mismatch-free verify.

    A malformed stored hash raises (``InvalidHashError``) instead of
    returning False: a corrupt column is a bug to surface, never a
    failed login (AGENTS I-1).
    """
    try:
        _hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash predates the currently pinned parameters."""
    return _hasher.check_needs_rehash(stored_hash)
