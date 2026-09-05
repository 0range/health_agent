from __future__ import annotations

import ipaddress
import json
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the local health-agent services."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("HEALTH_AGENT_ENV_FILE", ".env"),
        extra="ignore",
        populate_by_name=True,
    )

    postgres_host: str = Field(default="127.0.0.1", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=55432, validation_alias="POSTGRES_PORT")
    postgres_database: str = Field(
        default="health_agent", validation_alias="POSTGRES_DB"
    )
    postgres_user: str = Field(default="health_agent", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(
        default="health_agent", validation_alias="POSTGRES_PASSWORD"
    )
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    vault_root: Path = Field(default=Path("data/vault"), validation_alias="VAULT_ROOT")
    gmail_root: Path = Field(
        default=Path("data/google/gmail"), validation_alias="GMAIL_ROOT"
    )
    google_oauth_client_secrets: Path = Field(
        default=Path("data/secrets/google-oauth-client.json"),
        validation_alias="GOOGLE_OAUTH_CLIENT_SECRETS",
    )
    google_drive_root: Path = Field(
        default=Path("data/google-drive"), validation_alias="GOOGLE_DRIVE_ROOT"
    )
    google_drive_client_secrets: Path = Field(
        default=Path("data/secrets/google-drive-oauth-client.json"),
        validation_alias="GOOGLE_DRIVE_CLIENT_SECRETS",
    )
    google_drive_http_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        validation_alias="GOOGLE_DRIVE_HTTP_TIMEOUT_SECONDS",
    )
    google_sheets_root: Path = Field(
        default=Path("data/google-sheets"), validation_alias="GOOGLE_SHEETS_ROOT"
    )
    google_sheets_client_secrets: Path = Field(
        default=Path("data/secrets/google-oauth-client.json"),
        validation_alias="GOOGLE_SHEETS_CLIENT_SECRETS",
    )
    google_sheets_http_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        validation_alias="GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS",
    )
    temporary_root: Path = Field(
        default=Path("data/tmp"), validation_alias="TEMPORARY_ROOT"
    )
    gmail_max_attachment_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1,
        le=25 * 1024 * 1024,
        validation_alias="GMAIL_MAX_ATTACHMENT_BYTES",
    )
    gmail_http_timeout_seconds: int = Field(
        default=30, ge=1, le=300, validation_alias="GMAIL_HTTP_TIMEOUT_SECONDS"
    )
    telegram_root: Path = Field(
        default=Path("data/telegram"), validation_alias="TELEGRAM_ROOT"
    )
    telegram_token_file: Path | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN_FILE"
    )
    google_oauth_publishing_status: Literal["testing", "production", "internal"] = (
        Field(default="testing", validation_alias="GOOGLE_OAUTH_PUBLISHING_STATUS")
    )
    telegram_state_path: Path | None = Field(
        default=None, validation_alias="TELEGRAM_STATE_FILE"
    )
    telegram_staging_path: Path | None = Field(
        default=None, validation_alias="TELEGRAM_STAGING_ROOT"
    )
    connector_state_root: Path = Field(
        default=Path("data/connectors"), validation_alias="CONNECTOR_STATE_ROOT"
    )
    automation_root: Path = Field(
        default=Path("data/automation"), validation_alias="AUTOMATION_ROOT"
    )
    metabase_url: str = Field(
        default="http://127.0.0.1:53000", validation_alias="METABASE_URL"
    )
    metabase_admin_email: str = Field(
        default="health-agent@localhost", validation_alias="METABASE_ADMIN_EMAIL"
    )
    whoop_client_id: str | None = Field(
        default=None, validation_alias="WHOOP_CLIENT_ID"
    )
    whoop_client_secret: SecretStr | None = Field(
        default=None, validation_alias="WHOOP_CLIENT_SECRET"
    )
    whoop_client_credentials_file: Path = Field(
        default=Path(".tokens/whoop-client.json"),
        validation_alias="WHOOP_CLIENT_CREDENTIALS_FILE",
    )
    whoop_redirect_uri: str = Field(
        default="http://127.0.0.1:8765/whoop/callback",
        validation_alias="WHOOP_REDIRECT_URI",
    )
    whoop_token_root: Path = Field(
        default=Path(".tokens/whoop"), validation_alias="WHOOP_TOKEN_ROOT"
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY"
    )
    openai_api_key_file: Path = Field(
        default=Path(".tokens/openai-api-key"),
        validation_alias="OPENAI_API_KEY_FILE",
    )
    # gpt-5-mini is the deliberately stable, lower-latency default for this
    # bounded text-only health-question workflow. Deployments may override it.
    openai_model: str = Field(default="gpt-5-mini", validation_alias="OPENAI_MODEL")
    openai_max_output_tokens: int = Field(
        default=2_000,
        ge=64,
        le=8_000,
        validation_alias="OPENAI_MAX_OUTPUT_TOKENS",
    )
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = Field(
        default="low", validation_alias="OPENAI_REASONING_EFFORT"
    )
    panel_host: str = Field(default="127.0.0.1", validation_alias="PANEL_HOST")
    panel_port: int = Field(
        default=8766, ge=1, le=65535, validation_alias="PANEL_PORT"
    )

    @field_validator("panel_host")
    @classmethod
    def validate_panel_host(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("PANEL_HOST must be 127.0.0.1")
        return value

    @field_validator("metabase_url")
    @classmethod
    def validate_local_metabase_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("METABASE_URL must use http or https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "METABASE_URL must not contain credentials, query, or fragment"
            )
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
        if (
            not separator
            or not local
            or "." not in domain
            or any(char.isspace() for char in value)
        ):
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

    def load_whoop_client_credentials(self) -> tuple[str, SecretStr]:
        """Load WHOOP app credentials without including their values in errors."""
        if self.whoop_client_id is not None or self.whoop_client_secret is not None:
            if self.whoop_client_id is None or self.whoop_client_secret is None:
                raise ValueError(
                    "WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET must be set together"
                )
            return self.whoop_client_id, self.whoop_client_secret

        path = self.whoop_client_credentials_file
        try:
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                raise ValueError
            if stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise PermissionError
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            client_id = payload["client_id"]
            client_secret = payload["client_secret"]
            if not isinstance(client_id, str) or not client_id:
                raise TypeError
            if not isinstance(client_secret, str) or not client_secret:
                raise TypeError
        except FileNotFoundError as error:
            raise ValueError("WHOOP client credentials are not configured") from error
        except PermissionError as error:
            raise ValueError("WHOOP credentials file must have mode 0600") from error
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("WHOOP credentials file is invalid") from error
        return client_id, SecretStr(client_secret)

    def load_openai_api_key(self) -> SecretStr:
        """Return the configured API key without exposing it in failures.

        ``OPENAI_API_KEY`` is intentionally checked first.  The fallback is a
        single regular private file rather than an ambient keyring so callers
        can make this dependency explicit and auditable.
        """

        if self.openai_api_key is not None:
            value = self.openai_api_key.get_secret_value().strip()
            if value:
                return SecretStr(value)
            raise ValueError("OPENAI API key is not configured")

        path = self.openai_api_key_file
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r", encoding="utf-8") as key_file:
                file_stat = os.fstat(key_file.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError
                if stat.S_IMODE(file_stat.st_mode) != 0o600:
                    raise PermissionError
                value = key_file.read().strip()
            if not value:
                raise ValueError
        except FileNotFoundError as error:
            raise ValueError("OPENAI API key is not configured") from error
        except PermissionError as error:
            raise ValueError("OPENAI API key file must have mode 0600") from error
        except (OSError, ValueError) as error:
            raise ValueError("OPENAI API key file is invalid") from error
        return SecretStr(value)

    @model_validator(mode="after")
    def set_default_database_url(self) -> Settings:
        if self.database_url is None:
            password = quote(self.postgres_password, safe="")
            self.database_url = (
                f"postgresql+psycopg://{self.postgres_user}:{password}@{self.postgres_host}:"
                f"{self.postgres_port}/{self.postgres_database}"
            )
        return self

    @property
    def effective_telegram_token_file(self) -> Path:
        return self.telegram_token_file or self.telegram_root / "bot-token"

    @property
    def telegram_state_file(self) -> Path:
        return self.telegram_state_path or self.telegram_root / "state.sqlite3"

    @property
    def telegram_staging_root(self) -> Path:
        return self.telegram_staging_path or self.telegram_root / "staging"
