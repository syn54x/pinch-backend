"""The sync engine (M7 CP2, issue #34; PRD #31).

One cursor-based pass over a connection: provider calls first (network,
nothing written), then every effect in one database transaction — balance
entries, added transactions, cursor advance, health fields — then the
classification sweep deferred by the caller after commit.

Trigger-agnostic by design: manual refresh and the initial post-connect
sync call this today; the future nightly sweep is just another caller.

Error contract (PRD #31): auth-shaped provider errors mark the connection
``reauth_required`` and stop (retrying can't fix a dead login); transient
errors surface to the job runner to retry, and only exhaustion marks
``error`` — carrying the provider's error code, never more. Any successful
sync heals.

CP3 (#35) extends this pass with modified/removed handling — the batch
already carries both; this module deliberately applies only ``added``.

Stated boundary (I-1): transactions for provider accounts Pinch doesn't
hold — an account added at the bank after connect — are skipped, counted,
and logged; the cursor still advances. Adopting such an account later
(M8+ territory) therefore requires a cursor reset for a fresh backfill.
"""

import uuid  # runtime import: pydantic resolves the dataclass annotation at runtime

from ferro import transaction
from pydantic.dataclasses import dataclass

from pinch_backend import providers
from pinch_backend.crypto import decrypt_secret
from pinch_backend.imports.fingerprint import compute_fingerprint, normalize_description
from pinch_backend.models import (
    Account,
    BalanceEntry,
    BalanceSource,
    Connection,
    ConnectionStatus,
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger

log = get_logger(__name__)

AUTH_ERROR_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "ITEM_LOCKED",
    "USER_SETUP_REQUIRED",
    "PENDING_EXPIRATION",
    "PENDING_DISCONNECT",
    "INVALID_ACCESS_TOKEN",
}
"""Plaid codes that mean the login itself is dead — repair territory
(update-mode link token), not retry territory."""


@dataclass
class SyncOutcome:
    ledger_id: uuid.UUID | None = None
    created: int = 0

    @property
    def needs_classification(self) -> bool:
        return self.created > 0


async def _record_broken(connection: Connection, status: ConnectionStatus, code: str) -> None:
    """The two terminal health states share one shape: status + the
    provider's code — the only provider detail that ever lands in
    ``error_detail`` (PRD #31)."""
    connection.status = status
    connection.error_detail = code
    await connection.save()
    log.warning("sync.broken", connection_id=str(connection.id), status=status.value, code=code)


async def run_sync(connection_id: uuid.UUID, *, final_attempt: bool) -> SyncOutcome:
    """One sync pass. Raises ``providers.ProviderError`` on a transient
    failure when retries remain — the job runner's retry strategy is the
    backoff; on the final attempt the failure is recorded instead."""
    connection = await Connection.where(lambda c: c.id == connection_id).first()
    if connection is None or connection.encrypted_secret is None:
        # Deleted between defer and run (disconnect), or never completed
        # exchange — nothing to sync, nothing to record.
        return SyncOutcome()

    access_token = decrypt_secret(connection.encrypted_secret)
    provider = providers.get_provider()
    try:
        provider_accounts = await provider.get_accounts(access_token)
        batch = await provider.sync_transactions(access_token, connection.sync_cursor)
    except providers.ProviderError as error:
        if error.code in AUTH_ERROR_CODES:
            await _record_broken(connection, ConnectionStatus.REAUTH_REQUIRED, error.code)
            return SyncOutcome()
        if final_attempt:
            await _record_broken(connection, ConnectionStatus.ERROR, error.code)
            return SyncOutcome()
        raise  # transient with retries remaining: the runner's backoff handles it

    cid = connection.id
    accounts = await Account.where(lambda a: a.connection_id == cid).all()
    by_provider_id = {a.provider_account_id: a for a in accounts}
    account_ids = [a.id for a in accounts]
    existing = {
        t.provider_transaction_id
        for t in await Transaction.where(
            lambda t, ids=account_ids: t.account_id.in_(ids) & (t.provider_transaction_id != None)  # noqa: E711
        ).all()
    }

    created = 0
    skipped_unknown = 0
    async with transaction():
        for pa in provider_accounts:
            account = by_provider_id.get(pa.provider_account_id)
            if account is None or pa.balance_minor is None:
                continue
            await BalanceEntry.create(
                ledger_id=connection.ledger_id,  # ty: ignore[unresolved-attribute]
                account=account,
                amount_minor=pa.balance_minor,
                currency=account.currency,
                as_of=utcnow(),
                source=BalanceSource.PROVIDER,
            )
        for pt in batch.added:
            account = by_provider_id.get(pt.provider_account_id)
            if account is None:
                skipped_unknown += 1
                continue
            if pt.provider_transaction_id in existing:
                continue  # replayed page (crash between apply and cursor persist)
            await Transaction.create(
                ledger_id=connection.ledger_id,  # ty: ignore[unresolved-attribute]
                account=account,
                date=pt.date,
                amount_minor=pt.amount_minor,
                currency=pt.currency or account.currency,
                description_raw=pt.description,
                description_normalized=normalize_description(pt.description),
                fingerprint=compute_fingerprint(
                    account.id, pt.date, pt.amount_minor, pt.description
                ),
                provider_transaction_id=pt.provider_transaction_id,
                pending=pt.pending,
            )
            created += 1
        # CP3 (#35): batch.modified → in-place rewrites under the amount
        # contract; batch.removed → the import-undo dissolution seam.
        connection.sync_cursor = batch.next_cursor
        connection.status = ConnectionStatus.ACTIVE
        connection.error_detail = None
        connection.last_synced_at = utcnow()
        await connection.save()

    log.info(
        "sync.completed",
        connection_id=str(connection.id),
        created=created,
        skipped_unknown=skipped_unknown,
    )
    return SyncOutcome(
        ledger_id=connection.ledger_id,  # ty: ignore[unresolved-attribute]
        created=created,
    )
