from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

STAGING_PROJECT = "health-agent-staging"
PRODUCTION_POSTGRES_PORT = 55432
PRODUCTION_METABASE_PORT = 53000
PRODUCTION_DATABASES = {"health_agent", "metabase"}
_ENV_LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
_MANAGED_KEYS = {
    "CONNECTOR_STATE_ROOT",
    "DATABASE_URL",
    "HEALTH_AGENT_ENV_FILE",
    "METABASE_ADMIN_EMAIL",
    "METABASE_URL",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "STAGING_COMPOSE_PROJECT",
    "STAGING_METABASE_DB",
    "STAGING_METABASE_PORT",
    "TEMP_ROOT",
    "VAULT_ROOT",
    "WHOOP_CLIENT_CREDENTIALS_FILE",
    "WHOOP_CLIENT_ID",
    "WHOOP_CLIENT_SECRET",
    "WHOOP_REDIRECT_URI",
    "WHOOP_TOKEN_ROOT",
}


class StagingConfigurationError(ValueError):
    """Staging configuration would overlap production or is malformed."""


Runner = Callable[[Sequence[str], Path, dict[str, str]], None]


def _default_runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


@dataclass(frozen=True, slots=True)
class StagingEnvironment:
    root: Path
    env_file: Path
    values: dict[str, str]

    @classmethod
    def load(cls, root: Path, env_file: Path | None = None) -> StagingEnvironment:
        root = root.resolve()
        selected = env_file or (
            root / ".env.staging"
            if (root / ".env.staging").exists()
            else root / ".env.staging.example"
        )
        selected = selected.resolve()
        if selected == (root / ".env").resolve():
            raise StagingConfigurationError("staging cannot load production .env")
        values = _read_env_file(selected)
        environment = cls(root=root, env_file=selected, values=values)
        environment.validate()
        return environment

    def validate(self) -> None:
        value = self.values.__getitem__
        if value("STAGING_COMPOSE_PROJECT") != STAGING_PROJECT:
            raise StagingConfigurationError("staging Compose project name is fixed")

        postgres_port = _port(value("POSTGRES_PORT"), "POSTGRES_PORT")
        metabase_port = _port(value("STAGING_METABASE_PORT"), "STAGING_METABASE_PORT")
        if postgres_port == PRODUCTION_POSTGRES_PORT:
            raise StagingConfigurationError("staging PostgreSQL uses production port")
        if metabase_port == PRODUCTION_METABASE_PORT:
            raise StagingConfigurationError("staging Metabase uses production port")
        if postgres_port == metabase_port:
            raise StagingConfigurationError("staging ports must be distinct")
        if value("POSTGRES_DB") in PRODUCTION_DATABASES:
            raise StagingConfigurationError("staging uses a production database name")
        if value("STAGING_METABASE_DB") in PRODUCTION_DATABASES:
            raise StagingConfigurationError(
                "staging uses a production Metabase database"
            )
        if value("POSTGRES_USER") == "health_agent":
            raise StagingConfigurationError("staging uses the production database role")
        password = value("POSTGRES_PASSWORD")
        if len(password) < 6 or not any(character.isdigit() for character in password):
            raise StagingConfigurationError(
                "staging password must satisfy Metabase: six characters and a digit"
            )

        metabase_url = urlsplit(value("METABASE_URL"))
        if metabase_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise StagingConfigurationError("staging Metabase must be loopback-only")
        if metabase_url.port != metabase_port:
            raise StagingConfigurationError("METABASE_URL does not match staging port")

        database_url = self.values.get("DATABASE_URL")
        if database_url:
            parsed = urlsplit(database_url)
            if (
                parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port != postgres_port
                or parsed.path.lstrip("/") != value("POSTGRES_DB")
                or parsed.username != value("POSTGRES_USER")
            ):
                raise StagingConfigurationError(
                    "DATABASE_URL does not match the staging PostgreSQL target"
                )

        staging_root = (self.root / ".staging").resolve()
        root_keys = (
            "VAULT_ROOT",
            "TEMP_ROOT",
            "WHOOP_TOKEN_ROOT",
            "CONNECTOR_STATE_ROOT",
        )
        resolved_roots = [self.resolve_path(value(key)) for key in root_keys]
        if any(not path.is_relative_to(staging_root) for path in resolved_roots):
            raise StagingConfigurationError(
                "staging data roots must stay under .staging"
            )
        if len(set(resolved_roots)) != len(resolved_roots):
            raise StagingConfigurationError("staging data roots must be separate")
        for index, path in enumerate(resolved_roots):
            for other in resolved_roots[index + 1 :]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise StagingConfigurationError(
                        "staging data roots must not contain each other"
                    )

        credentials = self.resolve_path(value("WHOOP_CLIENT_CREDENTIALS_FILE"))
        production_credentials = (self.root / ".tokens/whoop-client.json").resolve()
        if credentials == production_credentials or not credentials.is_relative_to(
            staging_root
        ):
            raise StagingConfigurationError(
                "staging WHOOP credentials must be a separate .staging file"
            )

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        return (
            (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        )

    def subprocess_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        for key in _MANAGED_KEYS:
            environment.pop(key, None)
        environment.update(self.values)
        environment["HEALTH_AGENT_ENV_FILE"] = str(self.env_file)
        environment["COMPOSE_PROJECT_NAME"] = STAGING_PROJECT
        return environment

    def compose_command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            STAGING_PROJECT,
            "--env-file",
            str(self.env_file),
            "--file",
            str(self.root / "compose.staging.yaml"),
            *arguments,
        ]

    def application_command(self, *arguments: str) -> list[str]:
        return ["uv", "run", *arguments]

    def prepare_local_roots(self) -> None:
        staging_root = (self.root / ".staging").resolve()
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_root.chmod(0o700)
        for key in (
            "VAULT_ROOT",
            "TEMP_ROOT",
            "WHOOP_TOKEN_ROOT",
            "CONNECTOR_STATE_ROOT",
        ):
            path = self.resolve_path(self.values[key])
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        credential_parent = self.resolve_path(
            self.values["WHOOP_CLIENT_CREDENTIALS_FILE"]
        ).parent
        credential_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential_parent.chmod(0o700)


class StagingManager:
    def __init__(
        self, environment: StagingEnvironment, runner: Runner = _default_runner
    ):
        self.environment = environment
        self._runner = runner

    def start(self) -> None:
        self.environment.prepare_local_roots()
        self._run(self.environment.compose_command("up", "-d", "--wait"))
        self._run(self.environment.application_command("alembic", "upgrade", "head"))

    def status(self) -> None:
        self._run(self.environment.compose_command("ps"))

    def stop(self) -> None:
        self._run(self.environment.compose_command("stop"))

    def clean(self, confirmation: str) -> None:
        if confirmation != STAGING_PROJECT:
            raise StagingConfigurationError(
                f"clean requires --confirm {STAGING_PROJECT}"
            )
        self._run(
            self.environment.compose_command("down", "--volumes", "--remove-orphans")
        )

    def run_application(self, arguments: Sequence[str]) -> None:
        if not arguments:
            raise StagingConfigurationError(
                "staging run requires an application command"
            )
        self.environment.prepare_local_roots()
        self._run(self.environment.application_command(*arguments))

    def _run(self, command: Sequence[str]) -> None:
        self._runner(
            command,
            self.environment.root,
            self.environment.subprocess_environment(),
        )


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise StagingConfigurationError("staging env file cannot be read") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.fullmatch(line)
        if match is None:
            raise StagingConfigurationError(
                f"invalid staging env syntax on line {line_number}"
            )
        key, raw_value = match.groups()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    required = _MANAGED_KEYS.difference(
        {
            "DATABASE_URL",
            "HEALTH_AGENT_ENV_FILE",
            "WHOOP_CLIENT_ID",
            "WHOOP_CLIENT_SECRET",
        }
    )
    missing = sorted(required.difference(values))
    if missing:
        raise StagingConfigurationError(
            f"staging env is missing required keys: {', '.join(missing)}"
        )
    return values


def _port(raw_value: str, label: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as error:
        raise StagingConfigurationError(f"{label} must be an integer") from error
    if not 1 <= value <= 65535:
        raise StagingConfigurationError(f"{label} is outside the valid port range")
    return value
