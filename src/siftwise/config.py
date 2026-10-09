"""Settings loaded from environment variables (prefix ``SIFTWISE_``)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIFTWISE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://siftwise:siftwise@localhost:5432/siftwise"
    api_token: str | None = None
    max_upload_bytes: int = 50 * 1024 * 1024
    # test_stats are computed over this many days up to a repo's newest run (0 = all history).
    stats_window_days: int = Field(default=90, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
