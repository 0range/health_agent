from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

STAGING_PROJECT = "health-agent-staging"
PRODUCTION_PROJECT = "health-agent"
STAGING_METABASE_DATABASE = "metabase_staging"
PRODUCTION_POSTGRES_PORT = 55432
PRODUCTION_METABASE_PORT = 53000
PRODUCTION_DATABASES = {"health_agent", "metabase"}
_SYNTHETIC_STAGING_PASSWORD = "health_agent_staging_1"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_ENV_LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")

_PATH_DEFAULTS = {
    "VAULT_ROOT": "data/vault",
    "TEMPORARY_ROOT": "data/tmp",
    "CONNECTOR_STATE_ROOT": "data/connectors",
    "WHOOP_TOKEN_ROOT": ".tokens/whoop",
    "WHOOP_CLIENT_CREDENTIALS_FILE": ".tokens/whoop-client.json",
    "GMAIL_ROOT": "data/google/gmail",
    "GOOGLE_OAUTH_CLIENT_SECRETS": "data/secrets/google-oauth-client.json",
    "TELEGRAM_ROOT": "data/telegram",
}
_STAGING_PATH_KEYS = set(_PATH_DEFAULTS) | {
    "TELEGRAM_BOT_TOKEN_FILE",
    "TELEGRAM_STATE_FILE",
    "TELEGRAM_STAGING_ROOT",
}
_DIRECTORY_KEYS = {
    "VAULT_ROOT",
    "TEMPORARY_ROOT",
    "CONNECTOR_STATE_ROOT",
    "WHOOP_TOKEN_ROOT",
    "GMAIL_ROOT",
    "TELEGRAM_ROOT",
    "TELEGRAM_STAGING_ROOT",
}
_FILE_KEYS = _STAGING_PATH_KEYS - _DIRECTORY_KEYS
_REQUIRED_KEYS = {
    "CONNECTOR_STATE_ROOT",
    "GMAIL_ROOT",
    "GOOGLE_OAUTH_CLIENT_SECRETS",
    "METABASE_ADMIN_EMAIL",
    "METABASE_URL",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "STAGING_COMPOSE_PROJECT",
    "STAGING_METABASE_PORT",
    "TELEGRAM_BOT_TOKEN_FILE",
    "TELEGRAM_ROOT",
    "TELEGRAM_STATE_FILE",
    "TELEGRAM_STAGING_ROOT",
    "TEMPORARY_ROOT",
    "VAULT_ROOT",
    "WHOOP_CLIENT_CREDENTIALS_FILE",
    "WHOOP_REDIRECT_URI",
    "WHOOP_TOKEN_ROOT",
}
_OPTIONAL_KEYS = {
    "DATABASE_URL",
    "GMAIL_HTTP_TIMEOUT_SECONDS",
    "GMAIL_MAX_ATTACHMENT_BYTES",
    "GOOGLE_OAUTH_PUBLISHING_STATUS",
    "HEALTH_AGENT_ENV_FILE",
}
_MANAGED_KEYS = _REQUIRED_KEYS | _OPTIONAL_KEYS | {
    "WHOOP_CLIENT_ID",
    "WHOOP_CLIENT_SECRET",
    "STAGING_METABASE_DB",
}
_FORBIDDEN_STAGING_KEYS = {
    "WHOOP_CLIENT_ID",
    "WHOOP_CLIENT_SECRET",
    "STAGING_METABASE_DB",
}
_PRODUCTION_TARGET_KEYS = {
    "COMPOSE_PROJECT_NAME",
    "DATABASE_URL",
    "METABASE_URL",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
} | _STAGING_PATH_KEYS


class StagingConfigurationError(ValueError):
    """Staging configuration would overlap production or is malformed."""


Runner = Callable[[Sequence[str], Path, dict[str, str]], None]


def _default_runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    host: str
    port: int
    database: str
    user: str


@dataclass(frozen=True, slots=True)
class ProductionTargets:
    postgres_ports: frozenset[int]
    metabase_ports: frozenset[int]
    database_names: frozenset[str]
    database_users: frozenset[str]
    compose_projects: frozenset[str]
    paths: frozenset[Path]

    @classmethod
    def load(cls, root: Path) -> ProductionTargets:
        values = _production_values(root)
        postgres_port = _port(values.get("POSTGRES_PORT", "55432"), "production port")
        postgres_database = values.get("POSTGRES_DB", "health_agent")
        postgres_user = values.get("POSTGRES_USER", "health_agent")
        database_target = DatabaseTarget(
            host=_canonical_host(values.get("POSTGRES_HOST", "127.0.0.1")),
            port=postgres_port,
            database=postgres_database,
            user=postgres_user,
        )
        database_url = values.get("DATABASE_URL")
        if database_url:
            database_target = _database_target_from_url(
                database_url, "production database target"
            )

        metabase_url = urlsplit(
            values.get("METABASE_URL", f"http://127.0.0.1:{PRODUCTION_METABASE_PORT}")
        )
        try:
            metabase_port = metabase_url.port or 80
        except ValueError as error:
            raise StagingConfigurationError(
                "production Metabase target is invalid"
            ) from error

        path_values = dict(_PATH_DEFAULTS)
        path_values.update(
            {key: value for key, value in values.items() if key in _STAGING_PATH_KEYS}
        )
        telegram_root = path_values["TELEGRAM_ROOT"]
        path_values.setdefault("TELEGRAM_BOT_TOKEN_FILE", f"{telegram_root}/bot-token")
        path_values.setdefault("TELEGRAM_STATE_FILE", f"{telegram_root}/state.sqlite3")
        path_values.setdefault("TELEGRAM_STAGING_ROOT", f"{telegram_root}/staging")
        paths = frozenset(
            _filesystem_identity(_absolute_lexical(root / raw_path))
            for raw_path in path_values.values()
        )
        return cls(
            postgres_ports=frozenset(
                {PRODUCTION_POSTGRES_PORT, postgres_port, database_target.port}
            ),
            metabase_ports=frozenset({PRODUCTION_METABASE_PORT, metabase_port}),
            database_names=frozenset(
                PRODUCTION_DATABASES
                | {postgres_database, database_target.database}
            ),
            database_users=frozenset(
                {"health_agent", postgres_user, database_target.user}
            ),
            compose_projects=frozenset(
                {
                    PRODUCTION_PROJECT,
                    values.get("COMPOSE_PROJECT_NAME", PRODUCTION_PROJECT),
                }
            ),
            paths=paths,
        )


@dataclass(frozen=True, slots=True)
class StagingEnvironment:
    root: Path
    env_file: Path
    values: dict[str, str]
    production: ProductionTargets

    @classmethod
    def load(cls, root: Path, env_file: Path | None = None) -> StagingEnvironment:
        root = _absolute_lexical(Path(root))
        _require_regular_directory(root, "repository root")
        selected = env_file or (
            root / ".env.staging"
            if (root / ".env.staging").exists()
            else root / ".env.staging.example"
        )
        if not selected.is_absolute():
            selected = root / selected
        selected = _absolute_lexical(selected)
        if selected == _absolute_lexical(root / ".env"):
            raise StagingConfigurationError("staging cannot load production .env")
        _require_regular_file(selected, "staging env file")
        values = _read_env_file(selected)
        _validate_env_file_permissions(selected, values)
        environment = cls(
            root=root,
            env_file=selected,
            values=values,
            production=ProductionTargets.load(root),
        )
        environment.validate()
        return environment

    @property
    def staging_root(self) -> Path:
        return _absolute_lexical(self.root / ".staging")

    def validate(self) -> None:
        value = self.values.__getitem__
        if value("STAGING_COMPOSE_PROJECT") != STAGING_PROJECT:
            raise StagingConfigurationError("staging Compose project name is fixed")
        if STAGING_PROJECT in self.production.compose_projects:
            raise StagingConfigurationError(
                "staging Compose project overlaps the effective production project"
            )

        postgres_target = DatabaseTarget(
            host=_canonical_host(value("POSTGRES_HOST")),
            port=_port(value("POSTGRES_PORT"), "POSTGRES_PORT"),
            database=value("POSTGRES_DB"),
            user=value("POSTGRES_USER"),
        )
        if postgres_target.host != "loopback":
            raise StagingConfigurationError("staging PostgreSQL must be loopback-only")
        database_url = self.values.get("DATABASE_URL")
        if database_url:
            url_target = _database_target_from_url(database_url, "DATABASE_URL")
            if url_target.host != "loopback":
                raise StagingConfigurationError(
                    "staging PostgreSQL must be loopback-only"
                )
            if url_target != postgres_target:
                raise StagingConfigurationError(
                    "DATABASE_URL does not match the staging PostgreSQL target"
                )

        metabase_port = _port(value("STAGING_METABASE_PORT"), "STAGING_METABASE_PORT")
        if postgres_target.port in self.production.postgres_ports:
            raise StagingConfigurationError("staging PostgreSQL uses a production port")
        if metabase_port in self.production.metabase_ports:
            raise StagingConfigurationError("staging Metabase uses a production port")
        if postgres_target.port == metabase_port:
            raise StagingConfigurationError("staging ports must be distinct")
        if postgres_target.database in self.production.database_names:
            raise StagingConfigurationError("staging uses a production database name")
        if postgres_target.database == STAGING_METABASE_DATABASE:
            raise StagingConfigurationError(
                "staging application and Metabase databases must be distinct"
            )
        if STAGING_METABASE_DATABASE in self.production.database_names:
            raise StagingConfigurationError(
                "staging uses a production Metabase database"
            )
        if postgres_target.user in self.production.database_users:
            raise StagingConfigurationError("staging uses a production database role")
        password = value("POSTGRES_PASSWORD")
        if len(password) < 6 or not any(character.isdigit() for character in password):
            raise StagingConfigurationError(
                "staging password must satisfy Metabase: six characters and a digit"
            )

        metabase_url = urlsplit(value("METABASE_URL"))
        if _canonical_host(metabase_url.hostname or "") != "loopback":
            raise StagingConfigurationError("staging Metabase must be loopback-only")
        try:
            configured_metabase_port = metabase_url.port
        except ValueError as error:
            raise StagingConfigurationError("METABASE_URL has an invalid port") from error
        if configured_metabase_port != metabase_port:
            raise StagingConfigurationError("METABASE_URL does not match staging port")

        self._validate_paths()

    def _validate_paths(self) -> None:
        _reject_symlink_components(self.root, self.staging_root, final_kind="directory")
        for key in sorted(_STAGING_PATH_KEYS):
            path = self.resolve_path(self.values[key])
            if not path.is_relative_to(self.staging_root):
                raise StagingConfigurationError(
                    "staging data and credential paths must stay under .staging"
                )
            final_kind = "directory" if key in _DIRECTORY_KEYS else "file"
            _reject_symlink_components(self.root, path, final_kind=final_kind)
            identity = _filesystem_identity(path)
            if any(
                _paths_overlap(identity, production_path)
                for production_path in self.production.paths
            ):
                raise StagingConfigurationError(
                    "staging path overlaps the effective production configuration"
                )

        base_roots = [
            self.resolve_path(self.values[key])
            for key in (
                "VAULT_ROOT",
                "TEMPORARY_ROOT",
                "WHOOP_TOKEN_ROOT",
                "CONNECTOR_STATE_ROOT",
            )
        ]
        if len(set(base_roots)) != len(base_roots):
            raise StagingConfigurationError("staging data roots must be separate")
        for index, path in enumerate(base_roots):
            for other in base_roots[index + 1 :]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise StagingConfigurationError(
                        "staging data roots must not contain each other"
                    )

        credentials = self.resolve_path(self.values["WHOOP_CLIENT_CREDENTIALS_FILE"])
        if credentials.exists():
            _require_private_credentials_file(credentials)

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        return _absolute_lexical(self.root / path if not path.is_absolute() else path)

    def require_whoop_credentials(self) -> None:
        credentials = self.resolve_path(self.values["WHOOP_CLIENT_CREDENTIALS_FILE"])
        _reject_symlink_components(self.root, credentials, final_kind="file")
        _require_private_credentials_file(credentials)

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
        paths = [self.resolve_path(self.values[key]) for key in sorted(_DIRECTORY_KEYS)]
        paths.extend(
            self.resolve_path(self.values[key]).parent for key in sorted(_FILE_KEYS)
        )
        for path in sorted(set(paths), key=lambda candidate: len(candidate.parts)):
            _secure_directory_tree(self.root, path)


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
        if _is_whoop_secret_command(arguments):
            self.environment.require_whoop_credentials()
        self._run(self.environment.application_command(*arguments))

    def _run(self, command: Sequence[str]) -> None:
        self._runner(
            command,
            self.environment.root,
            self.environment.subprocess_environment(),
        )


def _read_env_file(path: Path) -> dict[str, str]:
    values = _parse_env_file(path)
    forbidden = sorted(_FORBIDDEN_STAGING_KEYS.intersection(values))
    if forbidden:
        raise StagingConfigurationError(
            f"staging env contains unsupported keys: {', '.join(forbidden)}"
        )
    unknown = sorted(set(values).difference(_REQUIRED_KEYS | _OPTIONAL_KEYS))
    if unknown:
        raise StagingConfigurationError(
            f"staging env contains unknown keys: {', '.join(unknown)}"
        )
    missing = sorted(_REQUIRED_KEYS.difference(values))
    if missing:
        raise StagingConfigurationError(
            f"staging env is missing required keys: {', '.join(missing)}"
        )
    return values


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise StagingConfigurationError("environment file cannot be read") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.fullmatch(line)
        if match is None:
            raise StagingConfigurationError(
                f"invalid environment syntax on line {line_number}"
            )
        key, raw_value = match.groups()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _production_values(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    production_env = root / ".env"
    if production_env.exists():
        _require_regular_file(production_env, "production env file")
        parsed = _parse_env_file(production_env)
        values.update(
            {key: value for key, value in parsed.items() if key in _PRODUCTION_TARGET_KEYS}
        )
    values.update(
        {
            key: os.environ[key]
            for key in _PRODUCTION_TARGET_KEYS
            if key in os.environ
        }
    )
    return values


def _validate_env_file_permissions(path: Path, values: Mapping[str, str]) -> None:
    contains_secret = (
        values.get("POSTGRES_PASSWORD") != _SYNTHETIC_STAGING_PASSWORD
        or "DATABASE_URL" in values
    )
    if contains_secret and stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise StagingConfigurationError(
            "staging env with a non-synthetic secret must have mode 0600"
        )


def _require_regular_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise StagingConfigurationError(f"{label} is unavailable") from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StagingConfigurationError(f"{label} must be a non-symlink directory")


def _require_regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise StagingConfigurationError(f"{label} is unavailable") from error
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise StagingConfigurationError(f"{label} must be a regular non-symlink file")


def _require_private_credentials_file(path: Path) -> None:
    _require_regular_file(path, "WHOOP credentials file")
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise StagingConfigurationError("WHOOP credentials file must have mode 0600")


def _reject_symlink_components(root: Path, path: Path, *, final_kind: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise StagingConfigurationError("staging path escapes repository root") from error
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StagingConfigurationError("staging path cannot be inspected") from error
        if stat.S_ISLNK(mode):
            raise StagingConfigurationError("staging paths must not contain symlinks")
        is_final = index == len(parts) - 1
        if not is_final and not stat.S_ISDIR(mode):
            raise StagingConfigurationError(
                "staging path has a non-directory component"
            )
        if is_final and final_kind == "directory" and not stat.S_ISDIR(mode):
            raise StagingConfigurationError("staging data root must be a directory")
        if is_final and final_kind == "file" and not stat.S_ISREG(mode):
            raise StagingConfigurationError("staging credential path must be a file")


def _secure_directory_tree(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise StagingConfigurationError("staging directory escapes repository root") from error
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, flags)
        descriptors.append(descriptor)
        for part in relative.parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(next_descriptor)
            descriptor = next_descriptor
            os.fchmod(descriptor, 0o700)
    except OSError as error:
        raise StagingConfigurationError(
            "staging directory could not be created without following symlinks"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _filesystem_identity(path: Path) -> Path:
    return path.resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _canonical_host(host: str) -> str:
    return "loopback" if host.lower() in _LOOPBACK_HOSTS else host.lower()


def _database_target_from_url(raw_url: str, label: str) -> DatabaseTarget:
    parsed = urlsplit(raw_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise StagingConfigurationError(f"{label} is invalid") from error
    database = parsed.path.lstrip("/")
    if not parsed.hostname or not port or not database or not parsed.username:
        raise StagingConfigurationError(f"{label} is invalid")
    return DatabaseTarget(
        host=_canonical_host(parsed.hostname),
        port=port,
        database=database,
        user=parsed.username,
    )


def _is_whoop_secret_command(arguments: Sequence[str]) -> bool:
    lowered = [argument.lower() for argument in arguments]
    return any(
        lowered[index] == "whoop" and lowered[index + 1] in {"auth", "sync"}
        for index in range(len(lowered) - 1)
    )


def _port(raw_value: str, label: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as error:
        raise StagingConfigurationError(f"{label} must be an integer") from error
    if not 1 <= value <= 65535:
        raise StagingConfigurationError(f"{label} is outside the valid port range")
    return value
