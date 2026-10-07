"""Settings loaded from environment variables (prefix ``SIEVE_``)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIEVE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://sieve:sieve@localhost:5432/sieve"
    api_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
