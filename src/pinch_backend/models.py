"""Domain core: Ledger, User, LedgerMember (PRD M1, issue #2).

Conventions established here bind every later table: UUIDv7 primary keys
generated app-side, UTC created/updated timestamps set app-side, and a
required ``ledger`` foreign key on all domain rows except User/LedgerMember —
the tenancy column (ADR-0002). All data access goes through ferro-orm
(ADR-0003).
"""

import uuid
from datetime import UTC, datetime
from datetime import date as CalendarDate
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, ClassVar, Optional

from ferro import BackRef, Field, ForeignKey, Model, Relation, transaction
from pydantic import field_validator

if TYPE_CHECKING:
    from pinch_backend.auth.models import (
        EmailVerificationToken,
        PasswordResetToken,
        PersonalAccessToken,
        Session,
    )


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


class BalanceSource(StrEnum):
    MANUAL = "manual"
    """Hand-entered by the user; providers supply entries too, later (M7+)."""


class ImportStatus(StrEnum):
    """The four locked lifecycle stages (PRD M4, CONTEXT.md: Importing).

    The synchronous v0 flow passes through MAPPED inside the mapping-confirm
    request (mapping stored, then rows parsed, one transaction), so the API
    observes uploaded → previewed; the stage is real state, not dead vocab.
    """

    UPLOADED = "uploaded"
    MAPPED = "mapped"
    PREVIEWED = "previewed"
    COMMITTED = "committed"


class RuleStatus(StrEnum):
    """Only ACTIVE rules are law (evaluated by the pipeline). PROPOSED and
    DISMISSED exist for CP4's promotion: a proposed rule awaits consent, a
    dismissed one is a tombstone that prevents eternal re-proposal."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    DISABLED = "disabled"
    DISMISSED = "dismissed"


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
    balance_entries: Relation[list["BalanceEntry"]] = BackRef()
    imports: Relation[list["Import"]] = BackRef()
    import_rows: Relation[list["ImportRow"]] = BackRef()
    import_profiles: Relation[list["ImportProfile"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()
    categories: Relation[list["Category"]] = BackRef()
    tags: Relation[list["Tag"]] = BackRef()
    transaction_tags: Relation[list["TransactionTag"]] = BackRef()
    rules: Relation[list["Rule"]] = BackRef()


class User(TimestampMixin, Model):
    """A person who signs in. Owns nothing financial directly — membership
    in a ledger is what grants access (ADR-0002)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    email: str = Field(unique=True)
    display_name: str
    primary_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    password_hash: str | None = Field(default=None, repr=False)
    """argon2id via pinch_backend.auth.passwords (M2, ADR-0005); None means
    no password login (e.g. a future social-only account)."""
    email_verified_at: datetime | None = None
    """Set once by the M2 verification flow; hosted instances may require
    it before domain data access (config, never a fork — ADR-0002)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    memberships: Relation[list["LedgerMember"]] = BackRef()
    # Auth rows (pinch_backend.auth.models) — declared here because ferro
    # requires the BackRef on the FK target; columns live on the auth tables.
    sessions: Relation[list["Session"]] = BackRef()
    email_verification_tokens: Relation[list["EmailVerificationToken"]] = BackRef()
    password_reset_tokens: Relation[list["PasswordResetToken"]] = BackRef()
    personal_access_tokens: Relation[list["PersonalAccessToken"]] = BackRef()

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

    balance_entries: Relation[list["BalanceEntry"]] = BackRef()
    imports: Relation[list["Import"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()


class BalanceEntry(TimestampMixin, Model):
    """One observed balance for an account at a point in time (PRD M4,
    issue #14).

    The account's current balance is its latest entry by ``as_of``.
    Transactions are records of money movement, never balance arithmetic:
    imported transactions do not move this number, and reconciling the two
    is M8's anchor-derivation design, not an omission (CONTEXT.md).
    """

    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (("account_id", "as_of"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="balance_entries", index=True)]
    """The tenancy column (ADR-0002), denormalized from the account so
    row-level security has one ownership column on every domain table."""
    account: Annotated[Account, ForeignKey(related_name="balance_entries", index=True)]
    amount_minor: int
    """Integer minor units + ISO 4217, always (CONTEXT.md: Money)."""
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    as_of: datetime
    source: BalanceSource = BalanceSource.MANUAL
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Import(TimestampMixin, Model):
    """A batch created by one file upload into a manual account, with the
    locked lifecycle uploaded → mapped → previewed → committed (CONTEXT.md).

    Nothing touches the ledger until commit; a committed batch is undoable
    as a unit, dead = gone (PRD M4). The raw bytes are retained so a
    corrected mapping can re-parse losslessly; rows live as long as their
    import (pruning policy is explicitly out of M4's scope).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="imports", index=True)]
    account: Annotated[Account, ForeignKey(related_name="imports", index=True)]
    status: ImportStatus = ImportStatus.UPLOADED
    filename: str
    file_bytes: bytes = Field(repr=False)
    suggested_mapping: dict | None = None
    """The MappingSpec the inferrer proposed at upload; null when nothing
    could be inferred. The API says "suggested", never how (PRD M4)."""
    confirmed_mapping: dict | None = None
    """The MappingSpec the user confirmed or corrected — the one rows were
    actually parsed with, and the payload a profile saves (CP3)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    rows: Relation[list["ImportRow"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()


class ImportRow(TimestampMixin, Model):
    """One raw record of an import plus its parsed values (PRD M4 #15).

    Rows are data — they get pagination and per-row overrides — and they
    are the preview: parsed values where parsing succeeded, per-row errors
    where it didn't. Invalid rows are excluded from commit; amounts that
    can't resolve exactly to minor units are invalid, never rounded (I-1).
    """

    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("import_batch_id", "row_index"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="import_rows", index=True)]
    import_batch: Annotated[Import, ForeignKey(related_name="rows", index=True)]
    """``import`` in the PRD's vocabulary; ``import_batch`` because Python
    reserves the keyword and CONTEXT.md defines an import as a batch."""
    row_index: int
    """Position among the file's data records, 0-based, header excluded."""
    raw_cells: list[str]
    date: CalendarDate | None = None
    """Aliased type: a field literally named ``date`` (the locked
    convention) shadows the ``datetime.date`` symbol in PEP 649's deferred
    annotation scope."""
    amount_minor: int | None = None
    description_raw: str | None = None
    valid: bool = False
    """Denormalized ``errors == []`` so commit and the preview counts can
    filter in SQL instead of loading every row."""
    errors: list[str] = Field(default_factory=list)
    duplicate: bool = False
    """Fingerprint collides with an existing transaction or another row in
    the same file (CONTEXT.md: Duplicate flag). Skipped at commit by
    default; the per-row override is the escape hatch, which is why
    skipping is a default and never silent."""
    fingerprint: str | None = None
    """Computed at parse for valid rows; the exact value the committed
    Transaction stores."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ImportProfile(TimestampMixin, Model):
    """A saved, user-confirmed column mapping for a file shape (CONTEXT.md):
    identity = normalized header tuple (casefold + trim, order-sensitive)
    + delimiter, scoped to the ledger.

    Auto-saved on successful commit of a headered file; a later commit
    confirming a different mapping for the same shape updates it (the
    freshest user confirmation wins). Headerless files never save or match
    one — no trustworthy shape identity. Undo of an import leaves its
    profile alone: the learning outlives the mistake (PRD M4).
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("ledger_id", "shape_key"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="import_profiles", index=True)]
    shape_key: str
    """The lookup key: delimiter + normalized headers, joined on ASCII
    unit separators (pinch_backend.imports.profiles.shape_key)."""
    header_tuple: list[str]
    """Normalized header cells, kept for display alongside the opaque key."""
    delimiter: str
    mapping: dict
    """The confirmed MappingSpec this shape parses with."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Transaction(TimestampMixin, Model):
    """A single money movement on an account (CONTEXT.md), minimal in M4:
    only what file imports produce.

    Locked conventions everything downstream bakes in (PRD M4): ``date`` is
    the institution's calendar date — timezone-free, never a localized
    timestamp; ``amount_minor`` is signed from the account's perspective —
    negative is money out.

    Field-ownership contract for every column M5+ adds: **source data**
    (everything on this table today: date, amount_minor, currency,
    description_raw, source_import, fingerprint) is owned by the
    transaction's origin — syncs and re-imports may rewrite it, users
    cannot. **User data** (M5+: category, tags, display name, notes,
    reviewed status) is owned by the user — syncs may never alter it, and
    a posted replacement inherits it (M7).
    """

    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("ledger_id", "date"),
        ("account_id", "fingerprint"),
        ("ledger_id", "description_normalized"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="transactions", index=True)]
    account: Annotated[Account, ForeignKey(related_name="transactions", index=True)]
    date: CalendarDate
    amount_minor: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    description_raw: str
    source_import: Annotated[Import | None, ForeignKey(related_name="transactions")] = None
    """The import that produced this row (the PRD's ``import`` FK); null on
    future provider-synced or hand-entered transactions."""
    fingerprint: str
    """Stored duplicate-detection hash (pinch_backend.imports.fingerprint):
    a pure function of retained source data, recomputable by design."""
    description_normalized: str
    """The **payee** (CONTEXT.md): NFKC → casefold → collapse whitespace →
    trim of description_raw, via imports.fingerprint.normalize_description.
    Source data, computed at write, indexed per-ledger for CP3 history
    matching. Non-null — first deploy runs on an empty schema, so no backfill."""
    category: Annotated[Optional["Category"], ForeignKey(related_name="transactions")] = None
    """User data (M5): the assigned category, or NULL for uncategorized."""
    display_name: str | None = None
    """User data: an override of description_raw for display; NULL shows the
    raw description (an override, never a copy — source rewrites shine through)."""
    notes: str | None = None
    """User data: free-form user annotation."""
    reviewed_at: datetime | None = None
    """User data: when the user cleared this from the review inbox; NULL means
    still in the inbox. M7 reopens review by nulling it."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    transaction_tags: Relation[list["TransactionTag"]] = BackRef()


class Category(TimestampMixin, Model):
    """A node in the ledger's editable classification taxonomy (PRD M5 #19).

    A transaction has at most one category and may be uncategorized (a NULL
    FK — the pipeline's bottom case and a legitimate reviewed state). Nesting
    is a plain self-referential parent FK; the two-level depth cap is an API
    validation (pinch_backend.taxonomy), never encoded here — the schema
    stays depth-agnostic so raising the cap is one constant.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="categories", index=True)]
    name: str
    parent: Annotated[Optional["Category"], ForeignKey(related_name="children")] = None
    """The verified ferro 0.16.1 self-FK spelling. NULL = a top-level node."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    children: Relation[list["Category"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()
    rules: Relation[list["Rule"]] = BackRef()


class Tag(TimestampMixin, Model):
    """A free-form, optional label; a transaction may carry many (CONTEXT.md).

    Created implicitly on first use. ``name_fold`` is the casefolded name and
    the uniqueness key, so "Vacation" and "vacation" never fork; the original
    casing is preserved in ``name`` for display.
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("ledger_id", "name_fold"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="tags", index=True)]
    name: str
    name_fold: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    transaction_tags: Relation[list["TransactionTag"]] = BackRef()


class TransactionTag(TimestampMixin, Model):
    """The transaction↔tag join (CONTEXT.md: a transaction carries many tags).

    Deleting a tag detaches it everywhere by removing these rows; tags are
    never load-bearing, so no reassignment machinery.
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("transaction_id", "tag_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="transaction_tags", index=True)]
    """The tenancy column (ADR-0002), denormalized so row-level security has
    one ownership column on every domain table."""
    transaction: Annotated["Transaction", ForeignKey(related_name="transaction_tags", index=True)]
    tag: Annotated[Tag, ForeignKey(related_name="transaction_tags", index=True)]
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Rule(TimestampMixin, Model):
    """A user-defined condition → action pair applied deterministically to
    incoming transactions (CONTEXT.md: Rule). Actions ride the proposal —
    a rule never writes user data directly (PRD M5, D13/D14).

    Conditions are an open, evolving vocabulary → a versioned pydantic spec
    stored as JSONB (pinch_backend.rules.spec.ConditionSpec — the MappingSpec
    precedent). Actions are typed columns: the category is the one action
    with referential-integrity stakes, so it is a real FK (a dangling
    category id is impossible by construction, and D4's delete-block is one
    indexed query). Tag names resolve to rows at apply time (CP3) — a rule
    may name a tag that doesn't exist yet.

    Evaluation order is creation order (uuid7), resolved in exactly one
    order_by at the evaluation site — nothing else attaches meaning to id
    order, so explicit priority later is one additive column.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    ledger: Annotated[Ledger, ForeignKey(related_name="rules", index=True)]
    status: RuleStatus = RuleStatus.ACTIVE
    """User-created rules are ACTIVE by authorship; PROPOSED is what CP4's
    promotion mints."""
    condition: dict
    """A validated ConditionSpec (versioned); never queried into — loaded
    and evaluated in Python only."""
    action_category: Annotated[Optional["Category"], ForeignKey(related_name="rules")] = None
    """Propose this category (indexed shadow FK: the D4 delete-block query)."""
    action_add_tags: list[str] = Field(default_factory=list)
    """Tag names to propose, unioned across matching rules (D13)."""
    action_rename_to: str | None = None
    """Proposed display_name override."""
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
    from pinch_backend.taxonomy import seed_default_taxonomy

    async with transaction():
        ledger = await Ledger.create(name=display_name)
        user = await User.create(
            email=email,
            display_name=display_name,
            primary_currency=primary_currency,
            password_hash=password_hash,
        )
        await LedgerMember.create(user=user, ledger=ledger, role=LedgerRole.OWNER)
        await seed_default_taxonomy(ledger)
    return user
