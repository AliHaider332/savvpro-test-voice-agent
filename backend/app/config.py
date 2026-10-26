"""Application settings loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Reads from environment, then backend/.env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="",
        description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/db",
    )
    database_url_test: str = Field(default="")
    clinic_timezone: str = Field(default="Asia/Karachi")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton so settings are parsed once per process."""
    return Settings()