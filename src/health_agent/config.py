from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the local health-agent services."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    postgres_host: str = Field(default="127.0.0.1", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=55432, validation_alias="POSTGRES_PORT")
    postgres_database: str = Field(default="health_agent", validation_alias="POSTGRES_DB")
    postgres_user: str = Field(default="health_agent", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(default="health_agent", validation_alias="POSTGRES_PASSWORD")
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    vault_root: Path = Field(default=Path("data/vault"), validation_alias="VAULT_ROOT")

    @model_validator(mode="after")
    def set_default_database_url(self) -> Settings:
        if self.database_url is None:
            password = quote(self.postgres_password, safe="")
            self.database_url = (
                f"postgresql+psycopg://{self.postgres_user}:{password}@{self.postgres_host}:"
                f"{self.postgres_port}/{self.postgres_database}"
            )
        return self
