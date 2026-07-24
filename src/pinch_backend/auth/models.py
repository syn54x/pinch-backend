"""Auth tables: Session and single-use flow tokens (PRD M2, issue #4).

Follows M1's model conventions (models.py): UUIDv7 primary keys, UTC
timestamps app-side, TimestampMixin. These are user-owned rows, not
ledger-owned — like User itself, they sit outside the tenancy column.

Secrets discipline (ADR-0002 — the code and its logs will be read): tables
store only SHA-256 hashes of tokens, never the secrets, and even the hashes
are excluded from ``repr`` so no log or error message can carry them.
"""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum
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


class PatScope(StrEnum):
    """v0 scopes (PRD M3): READ/WRITE are a rank — write implies read, so
    one column holds that truth and a guard check is a single comparison.
    PENNY (PRD M9) is the first orthogonal grant, a wire value only: it
    gates the chat endpoint (chat spends money, so no automation token gets
    it implicitly) and is stored as its own flag, never in the rank column.
    Per-resource scopes later are new machinery, not new values here."""

    READ = "read"
    WRITE = "write"
    PENNY = "penny"


class PersonalAccessToken(TimestampMixin, Model):
    """A named, scoped, revocable bearer credential (PRD M3, issue #10) —
    the second credential after the session cookie, reusing its discipline:
    opaque 256-bit secret shown exactly once, SHA-256 hash at rest,
    revocation deletes the row (dead = gone, same as Session; the audit
    trail is the structured ``auth.pat.revoked`` event).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    user: Annotated[User, ForeignKey(related_name="personal_access_tokens", index=True)]
    token_hash: str = Field(unique=True, repr=False)
    name: str
    """User-chosen label ("ci-script"); display-only, never unique."""
    scope: PatScope
    """The read/write rank only; PENNY never lands here."""
    penny_scope: bool = False
    """The orthogonal penny grant (PRD M9): may this token chat? Its own
    column because it is not a rank — write neither implies nor denies it."""
    display_prefix: str
    """Plaintext head of the secret (``pinch_pat_`` + a few characters) so
    the list view lets a user match a leaked token to a row. Far too short
    to help an attacker; the secret itself is never stored."""
    last_used_at: datetime | None = None
    """Touched when the PAT authenticates a request; the list view's
    "is this token still alive?" signal."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AuthAttempt(TimestampMixin, Model):
    """One row per guarded hit on an auth endpoint (PRD M2: in-Postgres
    rate limiting, no new infrastructure — ADR-0003).

    Limiting counts rows per key in a sliding window; stale rows are pruned
    as they age out. Keys are opaque strings ("login:email:a@b.c",
    "signup:ip:1.2.3.4") — no user FK, because the principal being limited
    (an email probe, an IP) usually isn't a user.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    key: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


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
