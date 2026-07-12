from litestar import Litestar, get

from pinch_backend import __version__
from pinch_backend.db import connect_database, disconnect_database
from pinch_backend.observability import configure_observability

configure_observability(service_name="pinch-backend-api")


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


app = Litestar(
    route_handlers=[health],
    on_startup=[connect_database],
    on_shutdown=[disconnect_database],
)
