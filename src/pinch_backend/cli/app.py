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
