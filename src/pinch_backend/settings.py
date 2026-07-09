from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PINCH_", env_file=".env", extra="ignore")

    debug: bool = False
    log_level: str = "INFO"
    environment: str = "development"


settings = Settings()
