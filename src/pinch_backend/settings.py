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


settings = Settings()
