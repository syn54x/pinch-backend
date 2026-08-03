from cyclopts import App

from pinch_backend import __version__
from pinch_backend.observability import configure_observability

configure_observability(service_name="pinch-backend")

app = App(
    name="pinch-dev",
    help="Pinch backend developer CLI (internal; the public CLI is pinch-cli)",
    version=__version__,
    version_flags=["--version", "-V"],
)


@app.default
def main() -> None:
    """Default command."""
    print("Hello from pinch-dev")


evals_app = App(
    name="evals",
    help="The evals harness (PRD M9): offline quality gate, never CI pass/fail. "
    "No prompt or model change merges without before/after numbers.",
)
app.command(evals_app)


def _warn_on_errors(errors: list[str], total: int) -> None:
    """Degrading to an abstain is the production shape, and an abstain
    scores — so a route that never reached the provider prints a plausible
    number instead of nothing. Say so, loudly, after the table: the reader
    is looking at scores, and the whole point is that the scores look
    fine. Distinct causes only; a broken route repeats one message per
    case."""
    import sys

    if not errors:
        return
    print(
        f"\nwarning: {len(errors)}/{total} case(s) errored and were scored as abstains "
        "— these numbers are not a quality result:",
        file=sys.stderr,
    )
    for message in list(dict.fromkeys(errors))[:3]:
        print(f"  {message}", file=sys.stderr)


@evals_app.command
def run(agent: str = "categorization", *, model: str | None = None) -> None:
    """Run AGENT's committed dataset against its configured model (or
    ``--model``), print the report, and record a Logfire experiment —
    accuracy, abstain rate, wrong rate, cost, latency over time."""
    import asyncio

    import logfire

    from pinch_backend.settings import settings

    knobs = {
        "categorization": settings.ai_categorization_model,
        "chat": settings.ai_chat_model,
        "mapping": settings.ai_mapping_model,
    }
    if agent not in knobs:
        raise SystemExit(f"Unknown agent {agent!r}; v0 evals cover: {', '.join(knobs)}")
    resolved = model or knobs[agent]
    if not resolved:
        raise SystemExit(
            f"No model: set PINCH_AI_{agent.upper()}_MODEL or pass --model "
            "(any pydantic-ai identifier, gateway or direct)."
        )
    logfire.instrument_pydantic_ai()

    async def _run_datasets_only() -> None:
        """categorization and mapping run on text alone — no database."""
        from pinch_backend.penny.evals import categorization_task, load_dataset, mapping_task

        errors: list[str] = []
        task = (
            mapping_task(resolved, errors)
            if agent == "mapping"
            else categorization_task(resolved, errors)
        )
        dataset = load_dataset(agent)
        report = await dataset.evaluate(task, name=f"{agent}:{resolved}")
        # Expected beside output: a score column alone says a case failed,
        # not what the model actually answered — the first thing you want
        # when reading a regression.
        report.print(include_input=False, include_expected_output=True, include_output=True)
        _warn_on_errors(errors, len(dataset.cases))

    async def _run_chat() -> None:
        """Chat golden tasks run the real capability stack: a throwaway
        sandbox user in THIS database, tools through the public API."""
        import ferro
        from pydantic_evals import Dataset

        from pinch_backend.api.app import create_app
        from pinch_backend.db import connect_database, disconnect_database
        from pinch_backend.jobs import close_job_app, ensure_job_schema, job_app, open_job_app
        from pinch_backend.penny.evals import EVALS_ROOT
        from pinch_backend.penny.evals_chat import ChatTrajectory, chat_task, provision_sandbox

        await connect_database()
        await open_job_app()
        await ensure_job_schema()
        try:
            # The ambient session covers the model-layer sandbox
            # provisioning; each in-process API self-call nests its own.
            async with ferro.engines.session():
                app_instance = create_app(manage_database=False)
                sandbox = await provision_sandbox(app_instance)
                # Drain the deferred classification/detection chain so the
                # sandbox has proposals and recurring series to talk about.
                await job_app.run_worker_async(
                    wait=False, listen_notify=False, install_signal_handlers=False
                )
                dataset = Dataset.from_file(
                    EVALS_ROOT / "chat" / "seed.yaml", custom_evaluator_types=[ChatTrajectory]
                )
                report = await dataset.evaluate(
                    chat_task(resolved, app_instance, sandbox),
                    name=f"chat:{resolved}",
                    max_concurrency=2,
                )
                report.print(include_input=True, include_output=False)
        finally:
            await close_job_app()
            await disconnect_database()

    asyncio.run(_run_chat() if agent == "chat" else _run_datasets_only())


@evals_app.command
def export(out: str = "evals/exports/correction-log.yaml") -> None:
    """Export this database's correction log as eval cases — user decisions
    only (auto-filed entries excluded by charter). Exports stay local:
    evals/exports/ is gitignored; promote scrubbed cases by hand."""
    import asyncio
    from pathlib import Path

    from pinch_backend.db import connect_database, disconnect_database
    from pinch_backend.penny.evals import export_correction_log

    async def _export() -> None:
        await connect_database()
        try:
            count = await export_correction_log(Path(out))
        finally:
            await disconnect_database()
        print(f"Wrote {count} cases to {out}")

    asyncio.run(_export())


@app.command
def worker() -> None:
    """Run the background-job worker (deployment shape: API + worker +
    Postgres, ADR-0006). Applies Procrastinate's schema on first run when
    PINCH_DATABASE_AUTO_MIGRATE is on."""
    import asyncio

    from pinch_backend.jobs import run_worker

    asyncio.run(run_worker())


@app.command
def reconcile() -> None:
    """Run one probe-then-decide reconcile pass immediately (M11 CP3):
    the deploy-day retrofit for pre-webhook Items and the dev tool for
    tunnel changes. Identical to the periodic task's pass — enqueued
    syncs still need the worker running to execute."""
    import asyncio

    from ferro import engines

    from pinch_backend.db import connect_database, disconnect_database
    from pinch_backend.jobs import close_job_app, ensure_job_schema, open_job_app
    from pinch_backend.reconcile import reconcile_pass
    from pinch_backend.settings import settings

    if not settings.plaid_configured:
        raise SystemExit("Plaid is not configured (PINCH_PLAID_CLIENT_ID / _SECRET)")

    async def _run() -> None:
        await connect_database()
        await open_job_app()
        await ensure_job_schema()
        try:
            async with engines.session():
                count = await reconcile_pass()
            print(f"examined {count} connection(s)")
        finally:
            await close_job_app()
            await disconnect_database()

    asyncio.run(_run())


@app.command(name="plaid-item")
def plaid_item(connection_id: str | None = None) -> None:
    """Probe Plaid's /item/get for one connection (or all of them):
    products, the per-product pull status, and the Item's standing error —
    the diagnostic for PRODUCT_NOT_READY mysteries (queued since the
    Stash QA session). Read-only; costs nothing, changes nothing."""
    import asyncio
    import json
    import uuid as uuid_module

    from ferro import engines

    from pinch_backend import providers
    from pinch_backend.crypto import decrypt_secret
    from pinch_backend.db import connect_database, disconnect_database
    from pinch_backend.models import Connection
    from pinch_backend.settings import settings

    if not settings.plaid_configured:
        raise SystemExit("Plaid is not configured (PINCH_PLAID_CLIENT_ID / _SECRET)")

    async def _probe() -> None:
        await connect_database()
        try:
            # ferro ≥0.13 routes operations through an explicit session —
            # in a one-shot process this is the only session there is.
            async with engines.session():
                await _probe_connections()
        finally:
            await disconnect_database()

    async def _probe_connections() -> None:
        query = Connection.where(lambda c: c.encrypted_secret != None)  # noqa: E711
        if connection_id:  # empty string = the justfile's "all" default
            wanted = uuid_module.UUID(connection_id)
            query = query.where(lambda c, w=wanted: c.id == w)
        connections = await query.all()
        if not connections:
            raise SystemExit("no matching connections with credentials")
        provider = providers.PlaidProvider(
            client_id=settings.plaid_client_id,
            secret=settings.plaid_secret,
            environment=settings.plaid_environment,
        )
        for connection in connections:
            print(
                f"connection {connection.id} · "
                f"{connection.institution_name or 'unnamed'} · "
                f"status={connection.status.value} "
                f"error={connection.error_detail or '—'} "
                f"investments_error={connection.investments_error_detail or '—'} "
                f"consent_required={connection.investments_consent_required}"
            )
            assert connection.encrypted_secret is not None
            try:
                report = await provider.get_item_status(decrypt_secret(connection.encrypted_secret))
            except providers.ProviderError as error:
                print(f"  probe failed: {error.code}")
                continue
            print(json.dumps(report, indent=2, default=str))

    asyncio.run(_probe())


if __name__ == "__main__":
    app()
