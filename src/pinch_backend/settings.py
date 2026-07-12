import secrets
from datetime import timedelta

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PINCH_", env_file=".env", extra="ignore")

    debug: bool = False
    log_level: str = "INFO"
    environment: str = "development"

    database_url: str = "sqlite:pinch.db?mode=rwc"
    database_auto_migrate: bool = True
    """Migrate the schema automatically on connect. Config, not a code fork
    (ADR-0002): hosted deploys disable it and use the Alembic bridge."""

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
    """Base for links in outbound mail (verification, reset)."""
    mailer_backend: str = "console"
    """v0 ships console delivery; SMTP is config later, never a fork."""
    turnstile_enabled: bool = False
    """Reserved (PRD M2): bot challenge integration is deferred to the
    hosted-deploy milestone. The flag exists so hosted config is additive;
    nothing reads it yet."""
    auth_rate_limit_per_email: int = 10
    """Attempts per email per window on credentialed endpoints."""
    auth_rate_limit_per_ip: int = 30
    """Attempts per client IP per window on auth endpoints."""
    auth_rate_limit_window: timedelta = timedelta(minutes=15)

    @model_validator(mode="after")
    def _resolve_secret_key(self) -> "Settings":
        if not self.secret_key:
            if self.environment != "development":
                raise ValueError("PINCH_SECRET_KEY is required outside development")
            self.secret_key = secrets.token_urlsafe(32)
        return self


settings = Settings()
