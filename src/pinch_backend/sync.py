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
from datetime import timedelta

from ferro import transaction
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from pinch_backend import providers
from pinch_backend.crypto import decrypt_secret
from pinch_backend.imports.fingerprint import compute_fingerprint, normalize_description
from pinch_backend.models import (
    Account,
    AccountKind,
    BalanceEntry,
    BalanceSource,
    Connection,
    ConnectionStatus,
    CorrectionActor,
    Holding,
    InvestmentActivity,
    InvestmentActivityType,
    Security,
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

CONSENT_ERROR_CODES = {"ADDITIONAL_CONSENT_REQUIRED"}
"""The Plaid code meaning the Item lacks investments consent (M10 CP2) —
the retrofit posture, healed by update-mode Link, never by retrying.
Deliberately narrow: INVALID_PRODUCT is excluded because it also fires
when the *operator's* Plaid client lacks the product, and a false consent
flag raises an un-healable Enable-investments prompt, while a missed
consent code merely shows as a visible investments error. Pinned for
build-time verification in PRD #72: the first real retrofit (the Stash
connection) confirms what production Plaid actually answers — adjust
here if it's different."""


@dataclass(config=ConfigDict(use_attribute_docstrings=True))
class SyncOutcome:
    ledger_id: uuid.UUID | None = None
    created: int = 0
    reopened: int = 0
    """Rows whose review was reopened (amount rewrites, dissolved-transfer
    counterparts) — back in the inbox, needing fresh proposals."""
    invalidated: int = 0
    """Unreviewed rows whose stale proposal died with an amount rewrite."""
    investments_due: bool = False
    """The caller should chain the investments job (M10): true after a
    banking success, and after a final-attempt transient failure — the
    bidirectional-isolation rule, once per ladder. Auth failures leave it
    false: a dead login is dead for both products."""

    @property
    def needs_classification(self) -> bool:
        return self.created > 0 or self.reopened > 0 or self.invalidated > 0


async def record_broken(connection: Connection, status: ConnectionStatus, code: str) -> None:
    """The two terminal health states share one shape: status + the
    provider's code — the only provider detail that ever lands in
    ``error_detail`` (PRD #31). Public since M11 CP2: the webhook
    receiver's ITEM events write broken states through this exact
    transition — the no-new-states law made literal."""
    connection.status = status
    connection.error_detail = code
    await connection.save()
    log.warning("sync.broken", connection_id=str(connection.id), status=status.value, code=code)


async def _sync_investments_guarded(
    connection: Connection,
    provider: "providers.SyncProvider",
    access_token: str,
    accounts: list[Account],
) -> None:
    """A code bug in the investments phase must record, never raise: the
    banking pass has already committed, so a raise makes the job runner
    replay it five times for nothing (smoke-test finding). Provider
    errors are the phase's own contract; anything else rolls back (the
    phase's transaction), logs with the traceback, and lands as
    INTERNAL_ERROR in the phase's health field."""
    try:
        await _sync_investments(connection, provider, access_token, accounts)
    except Exception:
        log.exception("sync.investments_phase_crashed", connection_id=str(connection.id))
        connection.investments_error_detail = "INTERNAL_ERROR"
        await connection.save()


async def _sync_investments(
    connection: Connection,
    provider: "providers.SyncProvider",
    access_token: str,
    accounts: list[Account],
) -> None:
    """The investments phase (M10 CP0, issue #73; ADR 0007) — after the
    banking commit, failure-isolated by construction.

    Gated on investment-kind accounts: the first holdings call is what
    starts Plaid's per-Item investments billing, so it fires only where
    there's an investment account to serve (PRD #72). Failures land in
    ``investments_error_detail`` and nothing else — never a raise (a
    retry would replay the already-committed banking pass), never
    ``status``/``error_detail``. Holdings are mirror-replaced: zero user
    data makes replacement safe.
    """
    investment_accounts = {
        a.provider_account_id: a
        for a in accounts
        if a.kind == AccountKind.INVESTMENT and a.provider_account_id is not None
    }
    if not investment_accounts:
        return
    ledger_id = connection.ledger_id  # ty: ignore[unresolved-attribute]
    window_end = utcnow().date()
    window_start = window_end - timedelta(days=providers.INVESTMENTS_WINDOW_DAYS)
    try:
        batch = await provider.get_holdings(access_token)
        activity_batch = await provider.get_investment_activities(
            access_token, start_date=window_start, end_date=window_end
        )
    except providers.ProviderError as error:
        if error.code in CONSENT_ERROR_CODES:
            # Not an error — a consent posture (the retrofit path):
            # update-mode Link collects it, the next sync clears it.
            connection.investments_consent_required = True
            connection.investments_error_detail = None
            await connection.save()
            log.info(
                "sync.investments_consent_required",
                connection_id=str(connection.id),
                code=error.code,
            )
            return
        connection.investments_error_detail = error.code
        await connection.save()
        log.warning("sync.investments_broken", connection_id=str(connection.id), code=error.code)
        return

    account_ids = [a.id for a in investment_accounts.values()]
    provider_securities: dict[str, providers.ProviderSecurity] = {}
    for ps in [*batch.securities, *activity_batch.securities]:
        provider_securities.setdefault(ps.provider_security_id, ps)
    skipped_unknown = 0
    async with transaction():
        # Queries before mutations (the ferro identity-map law): fetch
        # every existing set first, then write.
        securities_by_pid: dict[str, Security] = {}
        provider_sids = sorted(provider_securities)
        if provider_sids:
            for row in await Security.where(
                lambda s, lid=ledger_id, pids=provider_sids: (
                    (s.ledger_id == lid) & s.provider_security_id.in_(pids)
                )
            ).all():
                securities_by_pid[row.provider_security_id] = row
        holdings_by_key: dict[tuple[uuid.UUID, uuid.UUID], Holding] = {}
        for row in await Holding.where(lambda h, ids=account_ids: h.account_id.in_(ids)).all():
            holdings_by_key[(row.account_id, row.security_id)] = row  # ty: ignore[unresolved-attribute]
        activities_by_key: dict[tuple[uuid.UUID, str], InvestmentActivity] = {}
        for activity_row in await InvestmentActivity.where(
            lambda x, ids=account_ids: x.account_id.in_(ids)
        ).all():
            activities_by_key[
                (activity_row.account_id, activity_row.provider_activity_id)  # ty: ignore[unresolved-attribute]
            ] = activity_row

        for ps in provider_securities.values():
            row = securities_by_pid.get(ps.provider_security_id)
            if row is None:
                securities_by_pid[ps.provider_security_id] = await Security.create(
                    ledger_id=ledger_id,
                    provider_security_id=ps.provider_security_id,
                    name=ps.name,
                    ticker_symbol=ps.ticker_symbol,
                    type=ps.type,
                    is_cash_equivalent=ps.is_cash_equivalent,
                )
            elif (row.name, row.ticker_symbol, row.type, row.is_cash_equivalent) != (
                ps.name,
                ps.ticker_symbol,
                ps.type,
                ps.is_cash_equivalent,
            ):
                row.name = ps.name
                row.ticker_symbol = ps.ticker_symbol
                row.type = ps.type
                row.is_cash_equivalent = ps.is_cash_equivalent
                await row.save()

        present: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for ph in batch.holdings:
            account = investment_accounts.get(ph.provider_account_id)
            security = securities_by_pid.get(ph.provider_security_id)
            if account is None or security is None:
                # An account Pinch doesn't hold (I-1's stated boundary) or
                # a holding naming an unsent security — skip and count.
                skipped_unknown += 1
                continue
            key = (account.id, security.id)
            present.add(key)
            row = holdings_by_key.get(key)
            if row is None:
                await Holding.create(
                    ledger_id=ledger_id,
                    account=account,
                    security=security,
                    quantity=ph.quantity,
                    institution_price=ph.institution_price,
                    institution_price_as_of=ph.institution_price_as_of,
                    institution_value_minor=ph.institution_value_minor,
                    cost_basis_minor=ph.cost_basis_minor,
                    currency=ph.currency or account.currency,
                )
            else:
                row.quantity = ph.quantity
                row.institution_price = ph.institution_price
                row.institution_price_as_of = ph.institution_price_as_of
                row.institution_value_minor = ph.institution_value_minor
                row.cost_basis_minor = ph.cost_basis_minor
                row.currency = ph.currency or account.currency
                row.updated_at = utcnow()
                await row.save()

        doomed = [row.id for key, row in holdings_by_key.items() if key not in present]
        if doomed:
            # Sold out entirely: the position vanishes from holdings; the
            # story lives on as investment activity (CP1's record).
            await Holding.where(lambda h, ids=doomed: h.id.in_(ids)).delete()

        # Activities: stateless mirror with a retention floor (ADR 0007).
        # Exact-match within [window_start, window_end]; rows older than
        # the window are never touched — captured history is forever.
        activities_present: set[tuple[uuid.UUID, str]] = set()
        for pa in activity_batch.activities:
            account = investment_accounts.get(pa.provider_account_id)
            if account is None:
                skipped_unknown += 1
                continue
            try:
                activity_type = InvestmentActivityType(pa.type)
            except ValueError:
                # Provider vocabulary this build doesn't know — loud skip,
                # never a poisoned phase (the enum is Decision 14's law).
                # The key still shields any stored row from the mirror
                # sweep: Plaid IS returning it, so it must not be doomed.
                activities_present.add((account.id, pa.provider_activity_id))
                skipped_unknown += 1
                continue
            security = (
                securities_by_pid.get(pa.provider_security_id)
                if pa.provider_security_id is not None
                else None
            )
            key = (account.id, pa.provider_activity_id)
            activities_present.add(key)
            row = activities_by_key.get(key)
            if row is None:
                await InvestmentActivity.create(
                    ledger_id=ledger_id,
                    account=account,
                    security=security,
                    provider_activity_id=pa.provider_activity_id,
                    date=pa.date,
                    name=pa.name,
                    amount_minor=pa.amount_minor,
                    quantity=pa.quantity,
                    price=pa.price,
                    fees_minor=pa.fees_minor,
                    type=activity_type,
                    subtype=pa.subtype,
                    currency=pa.currency or account.currency,
                )
            else:
                # The FK column, never the relation: ferro exposes
                # relation fields as class-level descriptors on fetched
                # rows, so instance assignment raises (smoke-test finding).
                row.security_id = None if security is None else security.id  # ty: ignore[unresolved-attribute]
                row.date = pa.date
                row.name = pa.name
                row.amount_minor = pa.amount_minor
                row.quantity = pa.quantity
                row.price = pa.price
                row.fees_minor = pa.fees_minor
                row.type = activity_type
                row.subtype = pa.subtype
                row.currency = pa.currency or account.currency
                await row.save()
        doomed_activities = [
            row.id
            for key, row in activities_by_key.items()
            if key not in activities_present and row.date >= window_start
        ]
        if doomed_activities:
            await InvestmentActivity.where(lambda x, ids=doomed_activities: x.id.in_(ids)).delete()

        connection.investments_error_detail = None
        connection.investments_consent_required = False
        await connection.save()

    if skipped_unknown:
        # I-1's stated boundary (unknown account) or a provider anomaly
        # (holding naming an unsent security) — either way loud, not silent.
        log.warning(
            "sync.investments_skipped_unknown",
            connection_id=str(connection.id),
            skipped=skipped_unknown,
        )
    log.info(
        "sync.investments_completed",
        connection_id=str(connection.id),
        holdings=len(present),
        removed=len(doomed),
        activities=len(activities_present),
        activities_removed=len(doomed_activities),
        skipped_unknown=skipped_unknown,
    )


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
            # A dead login is dead for both products — no investments
            # attempt on a token repair can't be far behind anyway.
            await record_broken(connection, ConnectionStatus.REAUTH_REQUIRED, error.code)
            return SyncOutcome()
        if final_attempt:
            await record_broken(connection, ConnectionStatus.ERROR, error.code)
            # Isolation is bidirectional (post-QA fix on #72): a stuck
            # banking product must not hold investments hostage. The
            # motivating Stash Item is exactly this shape — transactions
            # perpetually PRODUCT_NOT_READY on a healthy token — and the
            # consent flag can only rise if the holdings call actually
            # fires. Once per ladder, at exhaustion, not per retry —
            # chained by the caller off ``investments_due``.
            return SyncOutcome(investments_due=True)
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
        ledger_id=ledger_id,
        created=created,
        reopened=reopened,
        invalidated=invalidated,
        investments_due=True,
    )


async def run_investments_sync(connection_id: uuid.UUID) -> None:
    """One investments pass (M10) — its own chained job, deferred after
    the banking pass the way classify_ledger is. Inline it sat between
    the banking commit and the classification defer, so a fresh Item's
    minutes-scale holdings extraction held every inbox proposal hostage
    (CI finding); chained, banking latency and the flywheel are
    untouched. Never raises: the guarded phase records its own health."""
    connection = await Connection.where(lambda c: c.id == connection_id).first()
    if connection is None or connection.encrypted_secret is None:
        return  # deleted between defer and run — nothing to record
    access_token = decrypt_secret(connection.encrypted_secret)
    provider = providers.get_provider()
    cid = connection.id
    accounts = await Account.where(lambda a, c=cid: a.connection_id == c).all()
    await _sync_investments_guarded(connection, provider, access_token, accounts)
