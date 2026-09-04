from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    metabase_url: str = Field(
        default="http://127.0.0.1:53000", validation_alias="METABASE_URL"
    )
    metabase_admin_email: str = Field(
        default="health-agent@localhost", validation_alias="METABASE_ADMIN_EMAIL"
    )
    whoop_client_id: str | None = Field(default=None, validation_alias="WHOOP_CLIENT_ID")
    whoop_client_secret: SecretStr | None = Field(
        default=None, validation_alias="WHOOP_CLIENT_SECRET"
    )
    whoop_redirect_uri: str = Field(
        default="http://127.0.0.1:8765/whoop/callback",
        validation_alias="WHOOP_REDIRECT_URI",
    )
    whoop_token_root: Path = Field(
        default=Path(".tokens/whoop"), validation_alias="WHOOP_TOKEN_ROOT"
    )

    @field_validator("metabase_url")
    @classmethod
    def validate_local_metabase_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("METABASE_URL must use http or https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("METABASE_URL must not contain credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("METABASE_URL must not contain a path")
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            is_loopback = parsed.hostname == "localhost"
        if not is_loopback:
            raise ValueError("METABASE_URL must use a loopback host")
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("METABASE_URL contains an invalid port") from error
        return value.rstrip("/")

    @field_validator("metabase_admin_email")
    @classmethod
    def validate_metabase_admin_email(cls, value: str) -> str:
        if value == "health-agent@localhost":
            return value
        local, separator, domain = value.rpartition("@")
        if not separator or not local or "." not in domain or any(char.isspace() for char in value):
            raise ValueError(
                "METABASE_ADMIN_EMAIL must be API-valid; only the local default is normalized"
            )
        return value

    @property
    def effective_metabase_admin_email(self) -> str:
        """Return the explicit Metabase login identity for the configured address."""
        if self.metabase_admin_email == "health-agent@localhost":
            return "health-agent@localhost.local"
        return self.metabase_admin_email

    @model_validator(mode="after")
    def set_default_database_url(self) -> Settings:
        if self.database_url is None:
            password = quote(self.postgres_password, safe="")
            self.database_url = (
                f"postgresql+psycopg://{self.postgres_user}:{password}@{self.postgres_host}:"
                f"{self.postgres_port}/{self.postgres_database}"
            )
        return self
