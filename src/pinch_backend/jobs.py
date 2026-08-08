"""Background jobs on Procrastinate (ADR-0006, pulled forward to M5 by the
approved epic amendment): Postgres-native queue, LISTEN/NOTIFY, retries,
locks. Deployment shape: one API process (defers), one worker process
(executes), one Postgres. Procrastinate manages its own tables and
connections — infrastructure, exempt from block-on-ferro (ADR-0003).

The API defers AFTER its ferro transaction commits: a phantom job from a
rollback is harmless to an idempotent sweep; a job racing data that isn't
visible yet is not. Tests replace the connector with
procrastinate.testing.InMemoryConnector (conftest, autouse).
"""

import uuid

import procrastinate
from ferro import engines

from pinch_backend.settings import settings


def _psycopg_conninfo(url: str) -> str:
    """ferro's DSN uses the postgres:// scheme and may carry ferro-only
    query params (ferro_search_path); psycopg wants postgresql:// and
    server params only."""
    _, _, rest = url.partition("://")
    base, _, query = rest.partition("?")
    params = [p for p in query.split("&") if p and not p.startswith("ferro_")]
    suffix = f"?{'&'.join(params)}" if params else ""
    return f"postgresql://{base}{suffix}"


job_app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=_psycopg_conninfo(settings.database_url))
)


async def open_job_app() -> None:
    await job_app.open_async()


async def close_job_app() -> None:
    await job_app.close_async()


async def ensure_job_schema() -> None:
    """Apply Procrastinate's schema when its tables are absent — the same
    config-not-fork stance as ferro's auto_migrate; hosted deploys disable
    the flag and run `procrastinate schema --apply` themselves."""
    if not settings.database_auto_migrate:
        return
    if await job_app.job_manager.check_connection_async():
        return
    await job_app.schema_manager.apply_schema_async()


@job_app.task(
    name="classification.classify_ledger",
    queue="classification",
    retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=2),
)
async def classify_ledger(ledger_id: str, auto_file_import_id: str | None = None) -> None:
    """The idempotent per-ledger sweep (PRD M5 D9). Args are strings —
    Procrastinate job payloads are JSON. Deferred with lock=ledger:{id} so
    two sweeps of one ledger serialize (the unique Proposal FK stays the
    correctness guard; the lock just cuts violation noise)."""
    from pinch_backend.classification.detection import detect_transfers
    from pinch_backend.classification.pipeline import sweep_ledger
    from pinch_backend.models import Ledger
    from pinch_backend.recurring import detect_recurring

    async with engines.session():
        if await Ledger.get_or_none(uuid.UUID(ledger_id)) is None:
            # Erased between defer and run (DELETE /me, issue #101) —
            # nothing to sweep, and the run_sync precedent: quiet no-op,
            # never five retried tracebacks over a deliberately gone row.
            return
        await sweep_ledger(
            uuid.UUID(ledger_id),
            auto_file_import_id=uuid.UUID(auto_file_import_id) if auto_file_import_id else None,
        )
        # The transfer detector runs after classification (M7 CP4): it sees
        # pairs, which no per-transaction stage can, and outranks whatever
        # shape the stages proposed for a matched pair.
        await detect_transfers(uuid.UUID(ledger_id))
        # The recurring detector rides the same pass (M8 CP3): cadences
        # live in history, which no per-transaction stage can see.
        await detect_recurring(uuid.UUID(ledger_id))


SYNC_MAX_ATTEMPTS = 5


@job_app.task(
    name="sync.sync_connection",
    queue="sync",
    retry=procrastinate.RetryStrategy(max_attempts=SYNC_MAX_ATTEMPTS, exponential_wait=2),
    pass_context=True,
)
async def sync_connection(context: procrastinate.JobContext, connection_id: str) -> None:
    """One connection sync (M7 CP2, #34). Deferred with lock=sync:{id} —
    ADR-0006's per-connection serialization: a manual refresh racing
    another sync queues, never conflicts. Transient provider errors raise
    into this retry strategy; the engine records the terminal states."""
    from pinch_backend.sync import run_sync

    attempts = context.job.attempts if context.job else 0
    async with engines.session():
        outcome = await run_sync(
            uuid.UUID(connection_id), final_attempt=attempts >= SYNC_MAX_ATTEMPTS - 1
        )
    if outcome.needs_classification and outcome.ledger_id is not None:
        # Defer-after-commit (module docstring): the session closed above,
        # so the sweep only ever races data it can see.
        await classify_ledger.configure(lock=f"ledger:{outcome.ledger_id}").defer_async(
            ledger_id=str(outcome.ledger_id)
        )
    if outcome.investments_due:
        # The investments phase is its own chained job (M10, post-CI
        # finding): inline it sat between the banking commit and the
        # classification defer, so a fresh Item's minutes-scale holdings
        # extraction held every inbox proposal hostage. Same lock as the
        # banking sync — serialized behind this job, never concurrent
        # with the next one.
        await sync_investments.configure(lock=f"sync:{connection_id}").defer_async(
            connection_id=connection_id
        )


@job_app.task(name="sync.sync_investments", queue="sync")
async def sync_investments(connection_id: str) -> None:
    """The investments phase (M10), chained from a completed banking pass
    the way classify_ledger is. No retry strategy on purpose: the phase
    records its failures on the connection (record-don't-retry, PRD #72)
    and the guarded runner never raises."""
    from pinch_backend.sync import run_investments_sync

    async with engines.session():
        await run_investments_sync(uuid.UUID(connection_id))


async def enqueue_sync_connection(connection_id) -> None:
    """One lock-serialized banking sync (ADR-0006: lock per connection) —
    the single spelling of the lock invariant every trigger shares:
    connect, manual refresh, doorbell, reconciler."""
    await sync_connection.configure(lock=f"sync:{connection_id}").defer_async(
        connection_id=str(connection_id)
    )


def reconcile_cron(hours: int) -> str:
    """PINCH_RECONCILE_INTERVAL_HOURS → cron: 24+ collapses to daily at
    midnight (cron's hour field can't express */24); anything smaller
    ticks on the hour every N hours."""
    if hours >= 24:
        return "0 0 * * *"
    if hours == 1:
        return "0 * * * *"
    return f"0 */{hours} * * *"


@job_app.periodic(cron=reconcile_cron(settings.reconcile_interval_hours), periodic_id="reconcile")
@job_app.task(name="sync.reconcile_connections", queue="sync")
async def reconcile_connections(timestamp: int) -> None:
    """The repo's first periodic task (M11 CP3): probe-then-decide over
    every due connection, any configured provider (M13 CP4). Keyless
    instances no-op — nothing to probe, and get_provider must never
    materialize without credentials; the pass itself filters
    per-provider, so a partially configured instance probes only what
    it can."""
    from pinch_backend.reconcile import reconcile_pass

    if not (settings.plaid_configured or settings.mx_configured):
        return
    async with engines.session():
        await reconcile_pass()


async def run_worker() -> None:
    """The worker process: ferro + the queue, then work until signalled."""
    from pinch_backend.db import connect_database

    await connect_database()
    async with job_app.open_async():
        await ensure_job_schema()
        await job_app.run_worker_async()
