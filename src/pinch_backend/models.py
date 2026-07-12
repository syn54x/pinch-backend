"""Domain core: Ledger, User, LedgerMember (PRD M1, issue #2).

Conventions established here bind every later table: UUIDv7 primary keys
generated app-side, UTC created/updated timestamps set app-side, and a
required ``ledger`` foreign key on all domain rows except User/LedgerMember —
the tenancy column (ADR-0002). All data access goes through ferro-orm
(ADR-0003).
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, ClassVar

from ferro import BackRef, Field, ForeignKey, Model, Relation, transaction
from pydantic import field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """Touch ``updated_at`` on every save.

    A plain mixin, not a ``Model`` base: ferro registers one table schema per
    concrete class, so shared *behavior* lives here while each model declares
    its own ``id`` / ``created_at`` / ``updated_at`` fields.
    """

    async def save(self, **kwargs) -> None:
        self.updated_at = utcnow()
        await super().save(**kwargs)  # ty: ignore[unresolved-attribute]


class LedgerRole(StrEnum):
    OWNER = "owner"


class AccountKind(StrEnum):
    DEPOSITORY = "depository"
    CREDIT = "credit"
    INVESTMENT = "investment"
    LOAN = "loan"
    ASSET = "asset"


class ConnectionProvider(StrEnum):
    PLAID = "plaid"


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    ERROR = "error"
    REAUTH_REQUIRED = "reauth_required"


class Ledger(TimestampMixin, Model):
    """The unit of data ownership and sharing (ADR-0002).

    All financial data belongs to a ledger, never directly to a user; in v0
    every user gets exactly one auto-created ledger.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    members: Relation[list["LedgerMember"]] = BackRef()
    accounts: Relation[list["Account"]] = BackRef()
    connections: Relation[list["Connection"]] = BackRef()


class User(TimestampMixin, Model):
    """A person who signs in. Owns nothing financial directly — membership
    in a ledger is what grants access (ADR-0002)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    email: str = Field(unique=True)
    display_name: str
    primary_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    password_hash: str | None = None
    """Column only — hashing, verification, and sessions are M2 (ADR-0005)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    memberships: Relation[list["LedgerMember"]] = BackRef()

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class LedgerMember(TimestampMixin, Model):
    """User↔ledger membership with a role, many-to-many from the start:
    a second household member is an INSERT, not a redesign."""

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (("user_id", "ledger_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    user: Annotated[User, ForeignKey(related_name="memberships", index=True)]
    ledger: Annotated[Ledger, ForeignKey(related_name="members", index=True)]
    role: LedgerRole = LedgerRole.OWNER
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Connection(TimestampMixin, Model):
    """A live link to an external data source (one Plaid Item = one
    institution login). Yields one or more accounts and owns credentials
    and sync state; manual accounts have no connection."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="connections", index=True)]
    provider: ConnectionProvider = ConnectionProvider.PLAID
    provider_item_id: str
    status: ConnectionStatus = ConnectionStatus.ACTIVE
    last_synced_at: datetime | None = None
    error_detail: str | None = None
    encrypted_secret: bytes | None = None
    """Opaque to M1 — the encryption utility lands with its first consumer (M7)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    accounts: Relation[list["Account"]] = BackRef()


class Account(TimestampMixin, Model):
    """Anything that holds value and contributes to net worth.

    One unified concept — checking, credit card, mortgage, brokerage, house —
    distinguished by ``kind``, so net worth is one sum over one table.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="accounts", index=True)]
    kind: AccountKind
    label: str
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    connection: Annotated[Connection | None, ForeignKey(related_name="accounts")] = None
    """Absent on a manual account."""
    provider_account_id: str | None = None
    archived: bool = False
    """Archive, don't delete: closed accounts keep their history."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


async def provision_user(
    *,
    email: str,
    display_name: str,
    primary_currency: str = "USD",
    password_hash: str | None = None,
) -> User:
    """Create a user with their auto-provisioned ledger and owner membership.

    One atomic domain operation (single transaction): either the user, their
    ledger, and the owner membership all exist afterwards, or none do. Lives
    here rather than in M2's signup flow because it is a tenancy invariant,
    not an auth flow.
    """
    async with transaction():
        ledger = await Ledger.create(name=display_name)
        user = await User.create(
            email=email,
            display_name=display_name,
            primary_currency=primary_currency,
            password_hash=password_hash,
        )
        await LedgerMember.create(user=user, ledger=ledger, role=LedgerRole.OWNER)
    return user
