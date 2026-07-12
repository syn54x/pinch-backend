"""Auth tables: Session and single-use flow tokens (PRD M2, issue #4).

Follows M1's model conventions (models.py): UUIDv7 primary keys, UTC
timestamps app-side, TimestampMixin. These are user-owned rows, not
ledger-owned — like User itself, they sit outside the tenancy column.

Secrets discipline (ADR-0002 — the code and its logs will be read): tables
store only SHA-256 hashes of tokens, never the secrets, and even the hashes
are excluded from ``repr`` so no log or error message can carry them.
"""

import uuid

# Runtime import despite TC003: ferro's metaclass evaluates model annotations
# eagerly at class definition (same constraint as the UP037 ignore in pyproject).
from datetime import datetime, timedelta  # noqa: TC003
from typing import Annotated

from ferro import Field, ForeignKey, Model

from pinch_backend.models import TimestampMixin, User, utcnow


class Session(TimestampMixin, Model):
    """A revocable server-side login session (ADR-0005 — no JWTs).

    A row that exists and passes both time checks is a live session; logout
    and revocation delete the row, so "dead server-side" means gone. The
    cookie carries the opaque secret; only its hash is stored here.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    user: Annotated[User, ForeignKey(related_name="sessions", index=True)]
    token_hash: str = Field(unique=True, repr=False)
    client_hint: str | None = None
    """Human-readable device hint shown in the session list (e.g. a
    trimmed User-Agent). Display-only; never trusted for anything."""
    last_seen_at: datetime = Field(default_factory=utcnow)
    """Touched on authenticated requests; drives idle expiry."""
    absolute_expires_at: datetime
    """Hard deadline set at issuance; activity never extends it."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def is_active(self, *, idle_ttl: timedelta, now: datetime) -> bool:
        """Both expiries must hold: recently used and inside the hard deadline."""
        return now < self.absolute_expires_at and now < self.last_seen_at + idle_ttl


class EmailVerificationToken(TimestampMixin, Model):
    """Single-use, short-TTL token mailed to prove address ownership.

    Consuming sets ``consumed_at`` rather than deleting: the row is the
    debuggable record of a flow ("the link you clicked was already used"),
    unlike a Session, whose existence is its meaning.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    user: Annotated[User, ForeignKey(related_name="email_verification_tokens", index=True)]
    token_hash: str = Field(unique=True, repr=False)
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class PasswordResetToken(TimestampMixin, Model):
    """Single-use, short-TTL token mailed for password recovery.

    Same shape as EmailVerificationToken, deliberately not abstracted:
    two flat tables beat a polymorphic "token" table (boring patterns,
    ADR-0005), and the flows diverge in M2 itself — completing a reset
    revokes the user's other sessions; verification does not.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    user: Annotated[User, ForeignKey(related_name="password_reset_tokens", index=True)]
    token_hash: str = Field(unique=True, repr=False)
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
