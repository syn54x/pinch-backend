import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# PINCH_ENV selects which dotenv file backs this process. It can only come
# from the process environment — it decides which file to read, so no file
# may set it. Default `local`: a machine that forgets the knob gets local
# credentials, never production ones (the failure mode is loud — a prod
# deploy without PINCH_ENV=prod finds no .env.prod values and refuses at
# startup — instead of quiet, which is how the 2026-08-02 cutover broke
# every sandbox-dependent e2e spec by overwriting the one shared .env).
_ENV_FILES = {"local": ".env.local", "dev": ".env.dev", "prod": ".env.prod"}
_pinch_env = os.environ.get("PINCH_ENV", "local")
if _pinch_env not in _ENV_FILES:
    raise ValueError(f"PINCH_ENV must be one of {sorted(_ENV_FILES)}, got {_pinch_env!r}")
_env_file = _ENV_FILES[_pinch_env]

# The selected file onto the process environment, not just onto Settings.
# Some credentials are deliberately not Pinch settings: pydantic-ai reads
# the provider key (ANTHROPIC_API_KEY, PYDANTIC_AI_GATEWAY_API_KEY)
# straight from os.environ, as does logfire (LOGFIRE_TOKEN) — the instance
# configures *which model*, never the credential (M9). pydantic-settings
# fills this class from the file and never touches the environment, so
# those keys reached the process only when something else exported them;
# `just` recipes do (`set dotenv-filename`), a bare `uv run pinch-dev` did
# not, and a missing key degrades to an abstain that still scores.
# The path is explicit: bare load_dotenv() resolves via find_dotenv(),
# which walks up from the calling module's directory and would disagree
# with env_file below about which file it means. override=False keeps the
# shell ahead of the file, the precedence pydantic-settings already uses.
load_dotenv(_env_file, override=False)
# Stamp the resolved value back so Settings.env below always reports the
# file actually loaded, even if a dotenv file smuggled in a PINCH_ENV.
os.environ["PINCH_ENV"] = _pinch_env

if Path(".env").exists():
    logging.getLogger(__name__).warning(
        "A bare .env exists but is no longer read; Pinch loads %s (PINCH_ENV=%s). "
        "Rename it to .env.local / .env.dev / .env.prod.",
        _env_file,
        _pinch_env,
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PINCH_", env_file=_env_file, extra="ignore")

    env: Literal["local", "dev", "prod"] = "local"
    """Which dotenv file backs this process (resolved above, before this
    class exists — the field is introspection, not a knob you can set in a
    file)."""
    debug: bool = False
    log_level: str = "INFO"
    environment: str = "development"

    database_url: str = "postgres://postgres:password@localhost:5432/pinch"
    """The one datastore (ADR-0003); default matches the local-pg dev
    container (started with POSTGRES_DB=pinch — see the README). The app
    gets its own named database rather than squatting in the `postgres`
    maintenance db. sqlite support was retired at M5 CP3: Procrastinate
    made Postgres load-bearing for the product's core loop, and a backend
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

    ai_chat_model: str = ""
    """Penny's chat agent: any pydantic-ai model identifier —
    ``anthropic:...`` with ``ANTHROPIC_API_KEY``, or ``gateway/anthropic:...``
    with ``PYDANTIC_AI_GATEWAY_API_KEY`` (PRD M9: one knob per agent; Pinch
    code never knows the gateway exists). Empty disables the agent: chat
    answers Penny-unavailable with a reason and nothing else is touched."""
    ai_categorization_model: str = ""
    """The categorization agent behind the classifier seam (M9 CP3).
    Empty ⇒ the AI stage abstains, exactly today's behavior."""
    ai_mapping_model: str = ""
    """The import-mapping agent behind the inferrer seam (M9 CP5).
    Empty ⇒ the deterministic heuristic stands alone."""

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
    plaid_redirect_uri: str = ""
    """Where OAuth institutions send the user back — typically
    {frontend_base_url}/connect/oauth-return (the frontend's fixed return
    route). Deliberately NOT derived: Plaid rejects link-token creation
    outright when the URI isn't registered in the dashboard, so setting
    this and registering it are one operator chore — empty (the default)
    omits it and keeps non-OAuth connects working (F2 enabler, #39)."""
    plaid_webhook_url: str = ""
    """Where Plaid rings the doorbell — the absolute URL of this instance's
    webhook receiver (e.g. https://pinch.example/webhooks/plaid). Required
    whenever Plaid is configured (ADR 0008): there is no webhook-less mode,
    so a Plaid instance that can't be reached must fail at startup, not
    silently never sync. Dev and self-host use a tunnel (ngrok)."""
    mx_client_id: str = ""
    """Instance-level, like everything MX (ADR 0009): MX holds no
    per-connection secret — the client_id/api_key pair plus guids is the
    whole credential story. Absent ⇒ MX endpoints refuse cleanly while
    every other provider stands (PRD #86 story 16)."""
    mx_api_key: str = ""
    mx_environment: Literal["sandbox", "production"] = "sandbox"
    """Same code path, different base URL (MX calls its sandbox the
    integration environment — int-api.mx.com); a typo fails at startup
    like every other misconfiguration."""
    mx_webhook_secret: str = ""
    """The per-instance secret URL segment authenticating MX's unsigned
    doorbells (/webhooks/mx/{secret}, ADR 0009): MX signs nothing, so a
    constant-time compare of this segment is the receiver's whole front
    door. Required whenever MX is configured (the ADR 0008 validator's
    MX analog, CP4 #91)."""
    reconcile_interval_hours: int = Field(default=24, ge=1)
    """The reconciler's tick (M11 CP3, ADR 0008): how often the periodic
    probe-then-decide pass runs. 24h default so dev machines aren't
    ticking hourly for no one; production sets 1. The per-connection
    examination cadence is a fixed 24h regardless — the tick only decides
    how promptly a connection crosses that line."""
    secret_encryption_key: str = ""
    """Fernet key encrypting provider access tokens at rest
    (`Fernet.generate_key()`); required the moment Plaid is configured.
    Single-key in v0 — rotation is a documented MultiFernet upgrade path."""

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)

    @property
    def mx_configured(self) -> bool:
        """MX needs no encryption key (PRD #86 story 17): the
        ``secret_encryption_key`` requirement stays Plaid-only because MX
        mints no per-connection secret to encrypt."""
        return bool(self.mx_client_id and self.mx_api_key)

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

    @model_validator(mode="after")
    def _require_webhook_url_with_plaid(self) -> "Settings":
        """Webhooks are required, not optional (ADR 0008): same loud-startup
        stance as the encryption key — a deployment that can't be rung must
        not discover it by never syncing."""
        if self.plaid_configured and not self.plaid_webhook_url:
            raise ValueError("PINCH_PLAID_WEBHOOK_URL is required when Plaid is configured")
        return self

    @model_validator(mode="after")
    def _require_webhook_secret_with_mx(self) -> "Settings":
        """ADR 0008's webhook validator to MX's honest extent (ADR 0009):
        the receiver URL itself is registered dashboard-side and the API
        can neither probe nor heal it, so startup can only require the
        secret that authenticates the doorbells — the setting's existence
        is the operator's attestation that /webhooks/mx/{secret} was
        registered. A registration mistake still can't be silent: MX
        self-aggregates nightly, so the reconciler cries webhook.missed
        daily at a URL nobody rings."""
        if self.mx_configured and not self.mx_webhook_secret:
            raise ValueError("PINCH_MX_WEBHOOK_SECRET is required when MX is configured")
        return self


settings = Settings()
