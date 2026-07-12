"""The login-method seam (PRD M2, ADR-0005).

Every way of proving an identity is a ``LoginMethod``: credentials in,
verified ``User`` (or None) out. All methods terminate in the same
``sessions.issue_session()`` — so "Sign in with Google" is a future
``register()`` call, never a redesign. v0 registers exactly one method:
password.
"""

import functools
import secrets
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Mapping

from pydantic import BaseModel, SecretStr

from pinch_backend.auth.passwords import hash_password, needs_rehash, verify_password
from pinch_backend.models import User


class LoginMethod(ABC):
    """One way to confirm an identity.

    ``authenticate`` returns the verified User or None — and None must be
    the *only* observable difference between every failure cause (unknown
    email, wrong password, passwordless account), including timing, so
    accounts cannot be enumerated (PRD user story 4).
    """

    name: ClassVar[str]

    @abstractmethod
    async def authenticate(self, credentials: Mapping[str, Any]) -> User | None:
        """Validate this method's own credential shape, then verify.

        Malformed credentials (wrong shape) raise ValidationError — a
        client bug, not a failed login.
        """


class PasswordCredentials(BaseModel):
    email: str
    password: SecretStr
    """SecretStr so a stray repr/log of the credentials never shows it."""


@functools.cache
def _decoy_hash() -> str:
    """A well-formed hash of an unknowable password, verified when the user
    doesn't exist (or has no password) so both failure paths cost one argon2
    verification. Cached: the cost must match real verifies, not include
    hashing setup."""
    return hash_password(secrets.token_urlsafe(32))


class PasswordMethod(LoginMethod):
    name = "password"

    async def authenticate(self, credentials: Mapping[str, Any]) -> User | None:
        creds = PasswordCredentials.model_validate(credentials)
        email = creds.email.strip().lower()
        user = await User.where(lambda u: u.email == email).first()

        if user is None or user.password_hash is None:
            verify_password(_decoy_hash(), creds.password.get_secret_value())
            return None
        if not verify_password(user.password_hash, creds.password.get_secret_value()):
            return None

        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(creds.password.get_secret_value())
            await user.save()
        return user


_registry: dict[str, LoginMethod] = {}


def register(method: LoginMethod) -> None:
    if method.name in _registry:
        raise ValueError(f"Login method '{method.name}' is already registered")
    _registry[method.name] = method


def get(name: str) -> LoginMethod:
    if name not in _registry:
        raise LookupError(f"No login method registered as '{name}'")
    return _registry[name]


def registered() -> tuple[str, ...]:
    return tuple(_registry)


register(PasswordMethod())
