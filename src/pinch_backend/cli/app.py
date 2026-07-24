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

    from pinch_backend.penny.evals import categorization_task, load_dataset
    from pinch_backend.settings import settings

    if agent != "categorization":
        raise SystemExit(f"Unknown agent {agent!r}; v0 evals cover: categorization")
    resolved = model or settings.ai_categorization_model
    if not resolved:
        raise SystemExit(
            "No model: set PINCH_AI_CATEGORIZATION_MODEL or pass --model "
            "(any pydantic-ai identifier, gateway or direct)."
        )
    logfire.instrument_pydantic_ai()

    async def _run() -> None:
        dataset = load_dataset(agent)
        report = await dataset.evaluate(categorization_task(resolved), name=f"{agent}:{resolved}")
        report.print(include_input=False, include_output=True)

    asyncio.run(_run())


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
