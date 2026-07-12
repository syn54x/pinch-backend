from litestar import Litestar, get
from litestar.config.csrf import CSRFConfig
from litestar.di import Provide

from pinch_backend import __version__
from pinch_backend.auth.guards import provide_current_ledger, provide_current_user
from pinch_backend.auth.routes import auth_router
from pinch_backend.db import connect_database, disconnect_database
from pinch_backend.observability import configure_observability
from pinch_backend.settings import settings

configure_observability(service_name="pinch-backend-api")


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def create_app(*, manage_database: bool = True) -> Litestar:
    """manage_database=False lets tests own the connection lifecycle while
    exercising the exact production app shape."""
    return Litestar(
        route_handlers=[health, auth_router],
        # CSRF on every unsafe method (PRD M2 story 14): the double-submit
        # cookie is issued on first response; clients echo it in x-csrftoken.
        csrf_config=CSRFConfig(
            secret=settings.secret_key,
            cookie_secure=settings.session_cookie_secure,
            cookie_samesite="lax",
        ),
        # Every router gets the acting user and ledger by declaring the
        # parameter (M2 story 13; M3 consumes this).
        dependencies={
            "current_user": Provide(provide_current_user),
            "current_ledger": Provide(provide_current_ledger),
        },
        on_startup=[connect_database] if manage_database else [],
        on_shutdown=[disconnect_database] if manage_database else [],
    )


app = create_app()
