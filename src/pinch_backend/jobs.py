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
    from pinch_backend.classification.pipeline import sweep_ledger

    async with engines.session():
        await sweep_ledger(
            uuid.UUID(ledger_id),
            auto_file_import_id=uuid.UUID(auto_file_import_id) if auto_file_import_id else None,
        )


async def run_worker() -> None:
    """The worker process: ferro + the queue, then work until signalled."""
    from pinch_backend.db import connect_database

    await connect_database()
    async with job_app.open_async():
        await ensure_job_schema()
        await job_app.run_worker_async()
