"""Full-account erasure (epic #97, issue #101) — Q11's *enforced* half.

A retention policy document alone does not erase anyone; this module is
the enforcement: revoke every provider-side connection first, then
hard-delete the ledger graph, the auth surface, and the user row in one
transaction. Nothing is retained — no soft-delete flag, no carve-outs.
The audit trail is the structured ``auth.me.deleted`` event, because
after this runs the rows themselves are gone.

Ordering law (the delete_connection contract, generalized): revoke
upstream first, delete locally second. Local rows deleted first would
orphan the provider-side access tokens and lose the ability to revoke
them at all — precisely the failure Plaid's questionnaire probes.

Failure semantics: all-or-nothing with idempotent retry. Every
revocation is attempted (one outage must not hide another's refusal);
any real failure aborts before a single local row dies, and the caller
retries. Retries converge because a provider already not knowing the
connection — ITEM_NOT_FOUND, MEMBER_NOT_FOUND, USER_NOT_FOUND — counts
as revoked.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from ferro import transaction

from pinch_backend import providers
from pinch_backend.auth.models import AuthAttempt, Session
from pinch_backend.crypto import decrypt_secret
from pinch_backend.models import (
    Category,
    Connection,
    ConnectionProvider,
    Enrollment,
    Ledger,
    LedgerMember,
    Rule,
    Transaction,
    User,
)
from pinch_backend.observability import get_logger

if TYPE_CHECKING:
    import uuid

log = get_logger(__name__)

_ALREADY_GONE = providers.ALREADY_DISCONNECTED_CODES | {"USER_NOT_FOUND"}
"""remove_item's already-disconnected codes plus delete_user's spelling:
every shape of "the provider has no such thing", which is this seat's
success case."""


@dataclass(frozen=True)
class RevocationFailure:
    """One provider-side object that refused revocation — the retryable
    remainder the route reports (codes only, the PRD #31 discipline)."""

    provider: ConnectionProvider
    code: str


class RevocationRefused(Exception):
    """At least one provider refused revocation. Nothing local was
    deleted; the caller may retry, and already-revoked providers now
    answering "gone" count as success, so retries converge."""

    def __init__(self, failures: list[RevocationFailure]) -> None:
        self.failures = failures
        super().__init__(", ".join(f"{f.provider.value}:{f.code}" for f in failures))


@dataclass(frozen=True)
class ErasureReceipt:
    """What the log line gets to say once the rows are gone."""

    ledger_id: uuid.UUID | None
    connections_revoked: int
    transactions_deleted: int
    sessions_revoked: int


@dataclass(frozen=True)
class RevocationSweep:
    """One pass over the provider-side surface: what was revoked, what
    refused, and exactly which rows the pass saw — the recheck set the
    delete transaction compares against (a connection minted mid-sweep
    must refuse, never cascade away unrevoked)."""

    revoked: int
    failures: list[RevocationFailure]
    connection_ids: frozenset[uuid.UUID]
    enrollment_ids: frozenset[uuid.UUID]


async def _revoke_providers(ledger_id: uuid.UUID) -> RevocationSweep:
    """Tear down every provider-side connection, then every MX user
    container (members do not cascade user-scope — providers.delete_user).

    An unconfigured provider is a refusal, not a skip: erasure reporting
    success while a live provider-side token exists is the one lie this
    endpoint must never tell. The operator restores the credentials and
    the user retries — observable and finite, unlike a silent orphan.
    An undecryptable secret (rotated/lost PINCH_SECRET_ENCRYPTION_KEY)
    is the same stance: a named refusal, not a crash — though recovering
    it needs the operator runbook (provider-dashboard removal), not a
    retry; S8 documents that escape hatch."""
    failures: list[RevocationFailure] = []
    revoked = 0
    connections = await Connection.where(lambda c, lid=ledger_id: c.ledger_id == lid).all()
    enrollments = await Enrollment.where(lambda e, lid=ledger_id: e.ledger_id == lid).all()
    mx_user_guid = next(
        (e.provider_user_id for e in enrollments if e.provider is ConnectionProvider.MX), None
    )
    for connection in connections:
        if not providers.provider_configured(connection.provider):
            failures.append(RevocationFailure(connection.provider, "PROVIDER_NOT_CONFIGURED"))
            continue
        if connection.provider is ConnectionProvider.MX:
            # An MX connection without its enrollment is corrupted state
            # (connects only ever ride an ensured enrollment) — but a
            # mid-sweep raise would hide every later connection's refusal,
            # so it reports as a named refusal instead of a 500.
            if mx_user_guid is None:
                failures.append(RevocationFailure(connection.provider, "ENROLLMENT_MISSING"))
                continue
            provider = providers.get_provider(
                connection.provider,
                user_guid=mx_user_guid,
                member_guid=connection.provider_item_id,
            )
        elif connection.encrypted_secret is not None:
            try:
                secret = decrypt_secret(connection.encrypted_secret)
            except InvalidToken:
                failures.append(RevocationFailure(connection.provider, "SECRET_UNDECRYPTABLE"))
                continue
            provider = providers.get_provider(connection.provider, secret=secret)
        else:
            # No provider-side binding ever existed (no secret to revoke).
            revoked += 1
            continue
        try:
            await provider.remove_item()
        except providers.ProviderError as error:
            if error.code not in _ALREADY_GONE:
                failures.append(RevocationFailure(connection.provider, error.code))
                continue
        revoked += 1
    for enrollment in enrollments:
        if enrollment.provider is not ConnectionProvider.MX:
            continue
        if not providers.provider_configured(enrollment.provider):
            failures.append(RevocationFailure(enrollment.provider, "PROVIDER_NOT_CONFIGURED"))
            continue
        # delete_user is MX-only plumbing outside the SyncProvider
        # protocol — get_mx_provider is its typed materialization.
        mx = providers.get_mx_provider(user_guid=enrollment.provider_user_id)
        try:
            await mx.delete_user()
        except providers.ProviderError as error:
            if error.code not in _ALREADY_GONE:
                failures.append(RevocationFailure(enrollment.provider, error.code))
    return RevocationSweep(
        revoked=revoked,
        failures=failures,
        connection_ids=frozenset(c.id for c in connections),
        enrollment_ids=frozenset(e.id for e in enrollments),
    )


async def erase_account(user: User) -> ErasureReceipt:
    """The whole erasure. Raises :class:`RevocationRefused` — with no
    local rows touched — if any provider refuses.

    Resolves membership directly instead of riding ``current_ledger``:
    the documented I-2 carve-out (AGENTS.md), because the hosted
    verification gate must never hold an unverified account hostage to
    its own deletion, and erasure is the one verb where "no ledger" is a
    state to proceed through, not a 500.

    v0 law: one user, one ledger. A ledger with any *other* member is a
    shared-ledger state this erasure was never designed against — loud
    refusal rather than destroying a housemate's data."""
    user_id = user.id
    email = user.email
    membership = await LedgerMember.where(lambda m, uid=user_id: m.user_id == uid).first()
    ledger_id = membership.ledger_id if membership else None
    connections_revoked = 0
    if ledger_id is not None:
        others = await LedgerMember.where(
            lambda m, lid=ledger_id, uid=user_id: (m.ledger_id == lid) & (m.user_id != uid)
        ).all()
        if others:
            raise RuntimeError(
                f"ledger {ledger_id} has other members; erasure of shared ledgers is undesigned"
            )
        sweep = await _revoke_providers(ledger_id)
        connections_revoked = sweep.revoked
        if sweep.failures:
            raise RevocationRefused(sweep.failures)
    transactions_deleted = 0
    async with transaction():
        if ledger_id is not None:
            lid = ledger_id
            # The revocation window is seconds wide (one provider HTTP
            # call per connection): a connection or enrollment minted
            # meanwhile — a second tab finishing a Link exchange — was
            # never revoked, and letting the cascade take it would orphan
            # a live provider-side token forever. Recheck-and-refuse; the
            # retry's sweep picks the newcomer up and converges.
            raced = [
                RevocationFailure(c.provider, "CREATED_DURING_ERASURE")
                for c in await Connection.where(lambda c, lid=lid: c.ledger_id == lid).all()
                if c.id not in sweep.connection_ids
            ] + [
                RevocationFailure(e.provider, "CREATED_DURING_ERASURE")
                for e in await Enrollment.where(lambda e, lid=lid: e.ledger_id == lid).all()
                if e.id not in sweep.enrollment_ids
            ]
            if raced:
                raise RevocationRefused(raced)
            # Two RESTRICT FKs would abort the ledger cascade mid-flight
            # (RESTRICT checks immediately, even when the referencing row
            # dies in the same statement): rules pin categories via
            # action_category, child categories pin their parents. Both
            # die first; everything else rides the DB cascade.
            await Rule.where(lambda r, lid=lid: r.ledger_id == lid).delete()
            await Category.where(
                lambda c, lid=lid: (c.ledger_id == lid) & (c.parent_id != None)  # noqa: E711
            ).delete()
            # Counted explicitly (the cascade reports nothing) — the log
            # line is the only audit trail this endpoint leaves.
            transactions_deleted = await Transaction.where(
                lambda t, lid=lid: t.ledger_id == lid
            ).delete()
            await Ledger.where(lambda ledger, lid=lid: ledger.id == lid).delete()
        # Rate-limit rows are keyed by opaque strings, not FKs
        # (auth.models.AuthAttempt) — the email-keyed shapes carry the
        # user's address and go with the account. IP-keyed rows are
        # pruned only when their key is hit again (rate_limit.py), so
        # they can outlive the account; S8's retention schedule names
        # them as a retained artifact with that lifetime.
        for key in (f"login:email:{email}", f"verify:email:{email}", f"reset:email:{email}"):
            await AuthAttempt.where(lambda a, k=key: a.key == k).delete()
        sessions_revoked = await Session.where(lambda s, uid=user_id: s.user_id == uid).delete()
        # The user row last: its cascade takes PATs and both token tables.
        await User.where(lambda u, uid=user_id: u.id == uid).delete()
    return ErasureReceipt(
        ledger_id=ledger_id,
        connections_revoked=connections_revoked,
        transactions_deleted=transactions_deleted,
        sessions_revoked=sessions_revoked,
    )
