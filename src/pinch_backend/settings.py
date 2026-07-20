import secrets
from datetime import timedelta
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PINCH_", env_file=".env", extra="ignore")

    debug: bool = False
    log_level: str = "INFO"
    environment: str = "development"

    database_url: str = "postgres://postgres:password@localhost:5432/postgres"
    """The one datastore (ADR-0003); default matches the local-pg dev
    container. sqlite support was retired at M5 CP3: Procrastinate made
    Postgres load-bearing for the product's core loop, and a backend
    nothing deploys on isn't worth a parallel execution story."""
    database_auto_migrate: bool = True
    """Migrate the schema automatically on connect. Config, not a code fork
    (ADR-0002): hosted deploys disable it and use the Alembic bridge."""
    database_migrate_updates: bool = True
    """Let auto_migrate ALTER existing tables (add/modify columns) on connect.
    On in development — Pinch is pre-deployment and wipe-and-reset is free;
    disabled for hosted deploys once the schema stabilizes (ADR-0002 config)."""
    database_migrate_destructive: bool = True
    """Let auto_migrate DROP columns/tables that no longer exist in the models.
    On in development for the same reason; there are no users to lose."""

    secret_key: str = ""
    """Signs the CSRF cookie (sessions themselves are database rows and need
    no signing key). Required outside development; development generates an
    ephemeral per-process key, which only invalidates in-flight CSRF tokens
    on restart."""
    session_cookie_name: str = "pinch_session"
    session_cookie_secure: bool = True
    """Secure default even in development — browsers exempt localhost.
    Self-hosters serving plain http on a LAN can switch it off (ADR-0002:
    config, never forks)."""
    session_idle_ttl: timedelta = timedelta(days=14)
    """A session unused this long is dead (M2 PRD: abandonment is bounded)."""
    session_absolute_ttl: timedelta = timedelta(days=90)
    """Hard session lifetime; activity never extends it."""

    signup_enabled: bool = True
    """Self-hosters may close signup after user #1 (PRD M2 story 11)."""
    verification_required: bool = False
    """Hosted instances gate domain data on a verified email (story 10);
    the default suits a single-user self-host."""
    breach_check_enabled: bool = True
    """Check new passwords against HIBP's k-anonymity range API (story 2).
    Fails open on network trouble — availability over ceremony — logged."""
    verification_token_ttl: timedelta = timedelta(hours=24)
    reset_token_ttl: timedelta = timedelta(hours=1)
    frontend_base_url: str = "http://localhost:5173"
    """The frontend's origin: base for links in outbound mail (verification,
    reset) and the CORS-allowed origin — one setting because they are
    genuinely the same place. Same-origin deployments simply never send
    cross-origin requests; the allowance is inert."""
    mailer_backend: str = "console"
    """v0 ships console delivery; SMTP is config later, never a fork."""
    turnstile_enabled: bool = False
    """Reserved (PRD M2): bot challenge integration is deferred to the
    hosted-deploy milestone. The flag exists so hosted config is additive;
    nothing reads it yet."""
    import_max_bytes: int = 5 * 1024 * 1024
    """Upload cap for CSV imports (PRD M4): the synchronous atomic commit
    is honest because bounded."""
    import_max_rows: int = 10_000
    """Rows per import (PRD M4). One bulk insert regardless of size:
    ferro chunks under backend bind-parameter limits since 0.16.1
    (ferro-orm#298)."""

    auth_rate_limit_per_email: int = 10
    """Attempts per email per window on credentialed endpoints."""
    auth_rate_limit_per_ip: int = 30
    """Attempts per client IP per window on auth endpoints."""
    auth_rate_limit_window: timedelta = timedelta(minutes=15)

    plaid_client_id: str = ""
    """Instance-level, like everything Plaid (PRD #31): hosted uses Pinch's
    developer account, a self-host uses the operator's. Absent ⇒ connection
    endpoints refuse cleanly and manual tracking is untouched."""
    plaid_secret: str = ""
    plaid_environment: Literal["sandbox", "production"] = "sandbox"
    """Same code path, different base URL; a typo fails at startup like
    every other misconfiguration, not at first request."""
    plaid_country_codes: list[str] = ["US"]
    """Passed to link-token creation; self-hosters elsewhere reconfigure."""
    secret_encryption_key: str = ""
    """Fernet key encrypting provider access tokens at rest
    (`Fernet.generate_key()`); required the moment Plaid is configured.
    Single-key in v0 — rotation is a documented MultiFernet upgrade path."""

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)

    @model_validator(mode="after")
    def _resolve_secret_key(self) -> "Settings":
        if not self.secret_key:
            if self.environment != "development":
                raise ValueError("PINCH_SECRET_KEY is required outside development")
            self.secret_key = secrets.token_urlsafe(32)
        return self

    @model_validator(mode="after")
    def _require_encryption_key_with_plaid(self) -> "Settings":
        """Fail at startup, not at first link: a half-configured instance
        must not discover the gap when a user connects a bank (PRD #31)."""
        if self.plaid_configured and not self.secret_encryption_key:
            raise ValueError("PINCH_SECRET_ENCRYPTION_KEY is required when Plaid is configured")
        return self


settings = Settings()
