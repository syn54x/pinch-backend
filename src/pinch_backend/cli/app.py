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

    async def _run_categorization() -> None:
        from pinch_backend.penny.evals import categorization_task, load_dataset

        dataset = load_dataset(agent)
        report = await dataset.evaluate(categorization_task(resolved), name=f"{agent}:{resolved}")
        report.print(include_input=False, include_output=True)

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

    asyncio.run(_run_chat() if agent == "chat" else _run_categorization())


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


if __name__ == "__main__":
    app()
