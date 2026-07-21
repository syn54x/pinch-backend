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

The replacement & removal contract (CP3, #35 — M6's ground-shifts):

- The provider communicates pending→posted as removed(pending) +
  added(posted, referencing the pending id) in one batch, so linked
  additions apply as **in-place rewrites first** and their removals are
  swallowed — or inheritance would never fire. ``modified`` rewrites the
  same way. Same Pinch row, always: user data inheritance is free because
  the row never moves.
- An amount rewrite invalidates what was built on the amount: split lines
  deleted, transfers dissolved with both sides reopened, review reopened,
  the affected decisions voided (actor=auto — source truth shifted, no
  human decided), re-classification follows. Anything else is cosmetic
  drift: source data updates, review stands.
- True removals retract through the same seam as import undo
  (pinch_backend.retraction): same event, different origin.

Stated boundary (I-1): transactions for provider accounts Pinch doesn't
hold — an account added at the bank after connect — are skipped, counted,
and logged; the cursor still advances. Adopting such an account later
(M8+ territory) therefore requires a cursor reset for a fresh backfill.
"""

import uuid  # runtime import: pydantic resolves the dataclass annotation at runtime

from ferro import transaction
from pydantic import ConfigDict
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
    CorrectionActor,
    SplitLine,
    Transaction,
    utcnow,
)
from pinch_backend.observability import get_logger
from pinch_backend.retraction import (
    delete_proposals_for,
    dissolve_transfers_touching,
    invalidate_mirrors_referencing,
    retract_transactions,
    void_decisions,
)

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


@dataclass(config=ConfigDict(use_attribute_docstrings=True))
class SyncOutcome:
    ledger_id: uuid.UUID | None = None
    created: int = 0
    reopened: int = 0
    """Rows whose review was reopened (amount rewrites, dissolved-transfer
    counterparts) — back in the inbox, needing fresh proposals."""
    invalidated: int = 0
    """Unreviewed rows whose stale proposal died with an amount rewrite."""

    @property
    def needs_classification(self) -> bool:
        return self.created > 0 or self.reopened > 0 or self.invalidated > 0


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
    ledger_id = connection.ledger_id  # ty: ignore[unresolved-attribute]
    accounts = await Account.where(lambda a: a.connection_id == cid).all()
    by_provider_id = {a.provider_account_id: a for a in accounts}
    for pa in provider_accounts:
        # Mask backfill/refresh (#39): pre-enabler rows gain their display
        # digits; a provider-side change updates them. Nullable nicety.
        account = by_provider_id.get(pa.provider_account_id)
        if account is not None and pa.mask is not None and account.mask != pa.mask:
            account.mask = pa.mask
            await account.save()
    account_ids = [a.id for a in accounts]
    relevant_ids = sorted(
        {pt.provider_transaction_id for pt in [*batch.added, *batch.modified]}
        | {
            pt.pending_provider_transaction_id
            for pt in batch.added
            if pt.pending_provider_transaction_id
        }
        | set(batch.removed)
    )
    rows_by_pid: dict[str, Transaction] = {}
    if relevant_ids:
        for row in await Transaction.where(
            lambda t, ids=account_ids, pids=relevant_ids: (
                t.account_id.in_(ids) & (t.provider_transaction_id.in_(pids))
            )
        ).all():
            if row.provider_transaction_id is not None:
                rows_by_pid[row.provider_transaction_id] = row

    created = rewritten = reopened = invalidated = removed_count = skipped_unknown = 0
    swallowed_removals: set[str] = set()

    async def rewrite_in_place(target: Transaction, pt: providers.ProviderTransaction) -> None:
        """Same Pinch row, new source truth. The amount contract: unchanged
        -> everything survives; changed -> what was built on it dies."""
        nonlocal rewritten, reopened, invalidated
        amount_changed = target.amount_minor != pt.amount_minor
        if amount_changed:
            tid = target.id
            await SplitLine.where(lambda ln, t=tid: ln.transaction_id == t).delete()
            # The target is excluded from the dissolve's reopen accounting —
            # its reopen (and full decision void) is handled right here, so
            # only the counterpart is the dissolve's to reopen.
            reopened += await dissolve_transfers_touching(
                ledger_id,
                [tid],
                exclude_members={tid},
                actor=CorrectionActor.AUTO,
                counterpart_reason="transfer dissolved: amount changed by provider sync",
            )
            await void_decisions(
                ledger_id,
                tid,
                actor=CorrectionActor.AUTO,
                reason="amount changed by provider sync",
            )
            await delete_proposals_for([tid])
            invalidated += await invalidate_mirrors_referencing([tid])
            if target.reviewed_at is not None:
                target.reviewed_at = None
                reopened += 1
            else:
                invalidated += 1
        target.date = pt.date
        target.amount_minor = pt.amount_minor
        target.currency = pt.currency or target.currency
        target.description_raw = pt.description
        target.description_normalized = normalize_description(pt.description)
        target.fingerprint = compute_fingerprint(
            target.account_id,  # ty: ignore[unresolved-attribute]
            pt.date,
            pt.amount_minor,
            pt.description,
        )
        target.provider_transaction_id = pt.provider_transaction_id
        target.pending = pt.pending
        await target.save()
        rewritten += 1

    async with transaction():
        for pa in provider_accounts:
            account = by_provider_id.get(pa.provider_account_id)
            if account is None or pa.balance_minor is None:
                continue
            await BalanceEntry.create(
                ledger_id=ledger_id,
                account=account,
                amount_minor=pa.balance_minor,
                currency=account.currency,
                as_of=utcnow(),
                source=BalanceSource.PROVIDER,
            )
        # Rewrites first (posted-replaces-pending swallows its removal;
        # `modified` upserts), then true removals, then fresh inserts.
        inserts: list[providers.ProviderTransaction] = []
        for pt in batch.added:
            if pt.provider_transaction_id in rows_by_pid:
                continue  # replayed page: this addition already applied
            pending_id = pt.pending_provider_transaction_id
            predecessor = rows_by_pid.get(pending_id) if pending_id else None
            if predecessor is not None and pending_id is not None:
                await rewrite_in_place(predecessor, pt)
                rows_by_pid[pt.provider_transaction_id] = predecessor
                swallowed_removals.add(pending_id)
            else:
                inserts.append(pt)
        for pt in batch.modified:
            target = rows_by_pid.get(pt.provider_transaction_id)
            if target is not None:
                await rewrite_in_place(target, pt)
            else:
                inserts.append(pt)  # modified-before-seen: upsert stance
        doomed_ids = [
            rows_by_pid[rid].id
            for rid in batch.removed
            if rid not in swallowed_removals and rid in rows_by_pid
        ]
        if doomed_ids:
            retract_reopened, retract_mirrors = await retract_transactions(
                ledger_id,
                doomed_ids,
                actor=CorrectionActor.AUTO,
                decision_reason="transaction removed by provider sync",
                counterpart_reason="transfer counterpart removed by provider sync",
            )
            reopened += retract_reopened
            invalidated += retract_mirrors
            removed_count = len(doomed_ids)
        for pt in inserts:
            account = by_provider_id.get(pt.provider_account_id)
            if account is None:
                skipped_unknown += 1
                continue
            await Transaction.create(
                ledger_id=ledger_id,
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
        connection.sync_cursor = batch.next_cursor
        connection.status = ConnectionStatus.ACTIVE
        connection.error_detail = None
        connection.last_synced_at = utcnow()
        await connection.save()

    if connection.institution_name is None:
        # Backfill for pre-enabler rows (#39) — best-effort, outside the
        # effects transaction: a nicety must never fail a sync.
        try:
            name = await provider.get_institution_name(access_token)
        except providers.ProviderError as error:
            log.info("connection.institution_backfill_failed", code=error.code)
            name = None
        if name is not None:
            connection.institution_name = name
            await connection.save()

    log.info(
        "sync.completed",
        connection_id=str(connection.id),
        created=created,
        rewritten=rewritten,
        removed=removed_count,
        reopened=reopened,
        skipped_unknown=skipped_unknown,
    )
    return SyncOutcome(
        ledger_id=ledger_id, created=created, reopened=reopened, invalidated=invalidated
    )
