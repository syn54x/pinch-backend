from litestar import Litestar, get
from litestar.config.cors import CORSConfig
from litestar.config.csrf import CSRFConfig
from litestar.di import Provide
from litestar.middleware import DefineMiddleware
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, SwaggerRenderPlugin, YamlRenderPlugin
from litestar.openapi.spec import Components, SecurityScheme

from pinch_backend import __version__
from pinch_backend.api.accounts import accounts_router
from pinch_backend.api.categories import categories_router
from pinch_backend.api.connections import connections_router
from pinch_backend.api.correction_log import correction_log_router
from pinch_backend.api.imports import import_profiles_router, imports_router
from pinch_backend.api.reports import reports_router
from pinch_backend.api.reviews import reviews_router
from pinch_backend.api.rules import rules_router
from pinch_backend.api.tags import tags_router
from pinch_backend.api.transactions import transactions_router
from pinch_backend.api.transfers import transfers_router
from pinch_backend.auth.csrf import CredentialAwareCSRFMiddleware
from pinch_backend.auth.guards import (
    provide_current_credential,
    provide_current_ledger,
    provide_current_session,
    provide_current_user,
)
from pinch_backend.auth.routes import auth_router
from pinch_backend.db import FerroSessionMiddleware, connect_database, disconnect_database
from pinch_backend.jobs import close_job_app, ensure_job_schema, open_job_app
from pinch_backend.observability import configure_observability
from pinch_backend.settings import settings

configure_observability(service_name="pinch-backend-api")

API_DESCRIPTION = """\
The Pinch developer API: anything the app can do, a script can do.

## Errors

Every failure answers one envelope, on every endpoint:

```json
{"status_code": 401, "detail": "Not authenticated", "extra": null}
```

- `status_code` — mirrors the HTTP status.
- `detail` — human-readable; stable enough to display, never parse.
- `extra` — optional machine-readable specifics (e.g. per-field
  validation errors); absent or null when there are none.

## Pagination

List endpoints take `?cursor=&limit=` and answer `{"items": [...],
"next_cursor": "..."}`, ordered stably by UUIDv7 id (creation order).
Pass `next_cursor` back as `cursor` for the next page; `null` means
exhausted. An unparseable cursor answers 400 in the error envelope.
"""


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def create_app(*, manage_database: bool = True) -> Litestar:
    """manage_database=False lets tests own the connection lifecycle while
    exercising the exact production app shape."""
    return Litestar(
        route_handlers=[
            health,
            auth_router,
            accounts_router,
            categories_router,
            connections_router,
            correction_log_router,
            imports_router,
            import_profiles_router,
            rules_router,
            tags_router,
            transactions_router,
            reports_router,
            transfers_router,
            reviews_router,
        ],
        # The served API contract (M3 story 7): the versioned path keeps the
        # document — like everything else public — under /api/v1.
        openapi_config=OpenAPIConfig(
            title="Pinch API",
            version=__version__,
            description=API_DESCRIPTION,
            path="/api/v1/schema",
            # Handler names ARE the client method names (frontend enabler):
            # a generated client reads `client.list_accounts(...)`, not
            # `client.ApiV1AccountsListAccounts(...)`. Uniqueness is enforced
            # at schema build, so a colliding name fails loudly here.
            operation_id_creator=lambda handler, http_method, path: handler.handler_name,
            # One interactive UI plus the raw document — a deliberate trim
            # of Litestar's default four-UI set. Swagger because it can
            # execute requests: Authorize with a PAT and try-it-out works.
            render_plugins=[SwaggerRenderPlugin(), YamlRenderPlugin(), ScalarRenderPlugin()],
            # Both credential schemes are contract (M3 story 7). Declared
            # API-wide as "either satisfies"; anonymous endpoints (signup,
            # login, health) simply ignore credentials.
            components=Components(
                security_schemes={
                    "bearerToken": SecurityScheme(
                        type="http",
                        scheme="bearer",
                        description="A personal access token (`pinch_pat_…`).",
                    ),
                    "sessionCookie": SecurityScheme(
                        type="apiKey",
                        security_scheme_in="cookie",
                        name=settings.session_cookie_name,
                        description="A browser session; unsafe methods also "
                        "require the x-csrftoken header.",
                    ),
                }
            ),
            security=[{"bearerToken": []}, {"sessionCookie": []}],
        ),
        # The browser frontend is a cross-origin caller in development
        # (Vite on :5173 → API on :8000) and possibly in deployment.
        # Credentialed CORS requires exact origins — never "*" — so the
        # allowance is the configured frontend origin and nothing else.
        cors_config=CORSConfig(
            allow_origins=[settings.frontend_base_url.rstrip("/")],
            allow_credentials=True,
            allow_headers=["content-type", "x-csrftoken", "authorization"],
        ),
        # CSRF on every unsafe cookie-credentialed request (PRD M2 story 14):
        # the double-submit cookie is issued on first response; clients echo
        # it in x-csrftoken. Bearer requests are exempt by construction (M3),
        # so the config feeds our credential-aware subclass instead of
        # Litestar's csrf_config hook (which would install the stock check).
        middleware=[
            # Outermost: every request runs inside one ferro session, the
            # app's own database scope (tests' ambient sessions just nest).
            DefineMiddleware(FerroSessionMiddleware),
            DefineMiddleware(
                CredentialAwareCSRFMiddleware,
                config=CSRFConfig(
                    secret=settings.secret_key,
                    cookie_secure=settings.session_cookie_secure,
                    cookie_samesite="lax",
                ),
            ),
        ],
        # Every router gets the acting credential, user, and ledger by
        # declaring the parameter (M2 story 13; M3 story 3).
        dependencies={
            "current_credential": Provide(provide_current_credential),
            "current_session": Provide(provide_current_session),
            "current_user": Provide(provide_current_user),
            "current_ledger": Provide(provide_current_ledger),
        },
        # ensure_job_schema after open_job_app: the API process may start
        # before any worker has ever run, so it can't rely on the worker
        # having applied Procrastinate's schema first (finding 12, M5 CP4
        # PR review) — without this, the first post-commit defer_async in a
        # fresh environment raises against missing tables, turning a
        # succeeded import commit into a client-visible 500.
        on_startup=[connect_database, open_job_app, ensure_job_schema] if manage_database else [],
        on_shutdown=[close_job_app, disconnect_database] if manage_database else [],
    )


app = create_app()
