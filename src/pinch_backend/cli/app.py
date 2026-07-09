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


if __name__ == "__main__":
    app()
