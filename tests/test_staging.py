from __future__ import annotations

import stat
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from health_agent.cli import app
from health_agent.staging import (
    PRODUCTION_METABASE_PORT,
    PRODUCTION_POSTGRES_PORT,
    PRODUCTION_PROJECT,
    STAGING_METABASE_DATABASE,
    STAGING_PROJECT,
    StagingConfigurationError,
    StagingEnvironment,
    StagingManager,
)


def example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in Path(".env.staging.example").read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", maxsplit=1)
            values[key] = value
    return values


def load_example() -> StagingEnvironment:
    return StagingEnvironment.load(Path.cwd(), Path(".env.staging.example"))


def write_override(
    root: Path, *, mode: int | None = None, **overrides: str
) -> Path:
    values = example_values()
    values.update(overrides)
    target = root / ".env.staging"
    target.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    if mode is None and (
        "DATABASE_URL" in overrides
        or overrides.get("POSTGRES_PASSWORD", "health_agent_staging_1")
        != "health_agent_staging_1"
    ):
        mode = 0o600
    if mode is not None:
        target.chmod(mode)
    return target


def test_example_is_separate_from_production_targets() -> None:
    staging = load_example()

    assert staging.values["STAGING_COMPOSE_PROJECT"] == STAGING_PROJECT
    assert int(staging.values["POSTGRES_PORT"]) != PRODUCTION_POSTGRES_PORT
    assert int(staging.values["STAGING_METABASE_PORT"]) != PRODUCTION_METABASE_PORT
    assert staging.values["POSTGRES_DB"] != "health_agent"
    assert STAGING_METABASE_DATABASE == "metabase_staging"
    assert (
        staging.resolve_path(staging.values["VAULT_ROOT"])
        != (Path.cwd() / "data/vault").resolve()
    )
    assert (
        staging.resolve_path(staging.values["WHOOP_CLIENT_CREDENTIALS_FILE"])
        != (Path.cwd() / ".tokens/whoop-client.json").resolve()
    )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        ("POSTGRES_PORT", "55432", "production port"),
        ("STAGING_METABASE_PORT", "53000", "production port"),
        ("POSTGRES_DB", "health_agent", "production database"),
        ("POSTGRES_PASSWORD", "no-digits", "six characters and a digit"),
        ("VAULT_ROOT", "data/vault", "under .staging"),
        (
            "WHOOP_CLIENT_CREDENTIALS_FILE",
            ".tokens/whoop-client.json",
            "under .staging",
        ),
    ),
)
def test_production_overlap_fails_closed(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    env_file = write_override(tmp_path, **{key: value})

    with pytest.raises(StagingConfigurationError, match=message):
        StagingEnvironment.load(tmp_path, env_file)


def test_postgres_host_must_be_loopback_without_database_url(tmp_path: Path) -> None:
    env_file = write_override(tmp_path, POSTGRES_HOST="database.example")

    with pytest.raises(StagingConfigurationError, match="loopback-only"):
        StagingEnvironment.load(tmp_path, env_file)


def test_database_url_must_be_loopback(tmp_path: Path) -> None:
    env_file = write_override(
        tmp_path,
        DATABASE_URL=(
            "postgresql+psycopg://health_agent_staging:password1@database.example:"
            "56432/health_agent_staging"
        ),
    )

    with pytest.raises(StagingConfigurationError, match="loopback-only"):
        StagingEnvironment.load(tmp_path, env_file)


def test_database_url_must_match_staging_database_and_port(tmp_path: Path) -> None:
    env_file = write_override(
        tmp_path,
        DATABASE_URL=(
            "postgresql+psycopg://health_agent_staging:password1@127.0.0.1:"
            "56433/health_agent_staging"
        ),
    )

    with pytest.raises(StagingConfigurationError, match="does not match"):
        StagingEnvironment.load(tmp_path, env_file)


@pytest.mark.parametrize(
    ("production_override", "message"),
    (
        ("POSTGRES_PORT=56432\n", "production port"),
        (
            (
                "DATABASE_URL=postgresql+psycopg://health_agent_staging:x@"
                "127.0.0.1:56432/health_agent_staging\n"
            ),
            "production port",
        ),
        ("POSTGRES_DB=health_agent_staging\n", "production database"),
        ("POSTGRES_USER=health_agent_staging\n", "production database role"),
        ("METABASE_URL=http://127.0.0.1:54000\n", "production port"),
        ("COMPOSE_PROJECT_NAME=health-agent-staging\n", "production project"),
        ("VAULT_ROOT=.staging/vault\n", "effective production"),
        ("VAULT_ROOT=.staging\n", "effective production"),
        ("VAULT_ROOT=.staging/vault/production\n", "effective production"),
    ),
)
def test_effective_production_overrides_are_collision_targets(
    tmp_path: Path, production_override: str, message: str
) -> None:
    (tmp_path / ".env").write_text(production_override, encoding="utf-8")
    env_file = write_override(tmp_path)

    with pytest.raises(StagingConfigurationError, match=message):
        StagingEnvironment.load(tmp_path, env_file)


def test_ambient_production_compose_project_collision_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", STAGING_PROJECT)
    env_file = write_override(tmp_path)

    with pytest.raises(StagingConfigurationError, match="production project"):
        StagingEnvironment.load(tmp_path, env_file)


@pytest.mark.parametrize(
    ("staging_override", "message"),
    (
        ({"POSTGRES_PORT": "55432"}, "production port"),
        (
            {
                "STAGING_METABASE_PORT": "53000",
                "METABASE_URL": "http://127.0.0.1:53000",
            },
            "production port",
        ),
        ({"POSTGRES_DB": "health_agent"}, "production database"),
        ({"POSTGRES_USER": "health_agent"}, "production database role"),
    ),
)
def test_production_application_overrides_do_not_replace_fixed_compose_targets(
    tmp_path: Path, staging_override: dict[str, str], message: str
) -> None:
    (tmp_path / ".env").write_text(
        (
            "POSTGRES_PORT=56433\n"
            "POSTGRES_DB=health_agent_other\n"
            "POSTGRES_USER=health_agent_other\n"
            "METABASE_URL=http://127.0.0.1:54001\n"
        ),
        encoding="utf-8",
    )
    env_file = write_override(tmp_path, **staging_override)

    with pytest.raises(StagingConfigurationError, match=message):
        StagingEnvironment.load(tmp_path, env_file)


def test_symlinked_staging_root_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    production_target = tmp_path / "data"
    production_target.mkdir(mode=0o755)
    (tmp_path / ".staging").symlink_to(production_target, target_is_directory=True)
    env_file = write_override(tmp_path)

    with pytest.raises(StagingConfigurationError, match="symlinks"):
        StagingEnvironment.load(tmp_path, env_file)

    assert stat.S_IMODE(production_target.stat().st_mode) == 0o755
    assert not (production_target / "vault").exists()


def test_nested_symlink_component_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "production-secrets"
    outside.mkdir()
    staging_root = tmp_path / ".staging"
    staging_root.mkdir()
    (staging_root / "credentials").symlink_to(outside, target_is_directory=True)
    env_file = write_override(tmp_path)

    with pytest.raises(StagingConfigurationError, match="symlinks"):
        StagingEnvironment.load(tmp_path, env_file)


def test_symlink_inserted_after_validation_is_not_followed(tmp_path: Path) -> None:
    env_file = write_override(tmp_path)
    staging = StagingEnvironment.load(tmp_path, env_file)
    outside = tmp_path / "production-vault"
    outside.mkdir(mode=0o755)
    (tmp_path / ".staging").mkdir()
    (tmp_path / ".staging/vault").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingConfigurationError, match="without following symlinks"):
        staging.prepare_local_roots()

    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert list(outside.iterdir()) == []


def test_lexical_path_escape_is_rejected(tmp_path: Path) -> None:
    env_file = write_override(tmp_path, VAULT_ROOT=".staging/../data/vault")

    with pytest.raises(StagingConfigurationError, match="under .staging"):
        StagingEnvironment.load(tmp_path, env_file)


@pytest.mark.parametrize("key", ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"))
def test_inline_whoop_credentials_are_rejected(tmp_path: Path, key: str) -> None:
    env_file = write_override(tmp_path, **{key: "must-not-be-inline"})

    with pytest.raises(StagingConfigurationError, match="unsupported keys"):
        StagingEnvironment.load(tmp_path, env_file)


def test_metabase_database_override_is_rejected(tmp_path: Path) -> None:
    env_file = write_override(tmp_path, STAGING_METABASE_DB="anything")

    with pytest.raises(StagingConfigurationError, match="unsupported keys"):
        StagingEnvironment.load(tmp_path, env_file)


def test_application_database_cannot_equal_fixed_metabase_database(
    tmp_path: Path,
) -> None:
    env_file = write_override(tmp_path, POSTGRES_DB=STAGING_METABASE_DATABASE)

    with pytest.raises(StagingConfigurationError, match="must be distinct"):
        StagingEnvironment.load(tmp_path, env_file)


def test_staging_cannot_load_production_env() -> None:
    with pytest.raises(StagingConfigurationError, match="production .env"):
        StagingEnvironment.load(Path.cwd(), Path(".env"))


def test_staging_env_must_not_be_a_symlink(tmp_path: Path) -> None:
    source = write_override(tmp_path)
    alias = tmp_path / "staging-link"
    alias.symlink_to(source)

    with pytest.raises(StagingConfigurationError, match="regular non-symlink"):
        StagingEnvironment.load(tmp_path, alias)


def test_cli_does_not_resolve_away_staging_env_symlink(tmp_path: Path) -> None:
    source = write_override(tmp_path)
    alias = tmp_path / "staging-link"
    alias.symlink_to(source)

    result = CliRunner().invoke(
        app, ["staging", "status", "--env-file", str(alias)]
    )

    assert result.exit_code != 0
    assert "regular non-symlink" in result.output


def test_staging_env_with_custom_password_must_be_private(tmp_path: Path) -> None:
    env_file = write_override(
        tmp_path, mode=0o644, POSTGRES_PASSWORD="not-synthetic-1"
    )

    with pytest.raises(StagingConfigurationError, match="mode 0600"):
        StagingEnvironment.load(tmp_path, env_file)


def test_missing_whoop_credentials_block_auth_but_not_status(tmp_path: Path) -> None:
    env_file = write_override(tmp_path)
    staging = StagingEnvironment.load(tmp_path, env_file)
    calls: list[list[str]] = []

    def runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
        calls.append(list(command))

    manager = StagingManager(staging, runner)
    manager.run_application(("health-agent", "whoop", "status"))
    with pytest.raises(StagingConfigurationError, match="unavailable"):
        manager.run_application(("health-agent", "whoop", "auth"))

    assert calls == [
        staging.application_command("health-agent", "whoop", "status")
    ]


def test_whoop_credentials_must_be_regular_private_file(tmp_path: Path) -> None:
    env_file = write_override(tmp_path)
    staging = StagingEnvironment.load(tmp_path, env_file)
    credential_file = staging.resolve_path(
        staging.values["WHOOP_CLIENT_CREDENTIALS_FILE"]
    )
    credential_file.parent.mkdir(parents=True)
    credential_file.write_text("{}", encoding="utf-8")
    credential_file.chmod(0o644)

    with pytest.raises(StagingConfigurationError, match="mode 0600"):
        staging.require_whoop_credentials()


def test_whoop_credentials_symlink_is_rejected(tmp_path: Path) -> None:
    env_file = write_override(tmp_path)
    credential_file = tmp_path / ".staging/credentials/whoop-client.json"
    credential_file.parent.mkdir(parents=True)
    source = tmp_path / "actual-credentials"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    credential_file.symlink_to(source)

    with pytest.raises(StagingConfigurationError, match="symlinks"):
        StagingEnvironment.load(tmp_path, env_file)


def test_private_whoop_credentials_allow_sync_command(tmp_path: Path) -> None:
    env_file = write_override(tmp_path)
    staging = StagingEnvironment.load(tmp_path, env_file)
    credential_file = staging.resolve_path(
        staging.values["WHOOP_CLIENT_CREDENTIALS_FILE"]
    )
    credential_file.parent.mkdir(parents=True)
    credential_file.write_text("{}", encoding="utf-8")
    credential_file.chmod(0o600)
    calls: list[list[str]] = []

    def runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
        calls.append(list(command))

    StagingManager(staging, runner).run_application(
        ("health-agent", "whoop", "sync")
    )

    assert calls == [staging.application_command("health-agent", "whoop", "sync")]


def test_commands_pin_project_file_and_preserve_volumes_by_default() -> None:
    staging = load_example()
    calls: list[list[str]] = []

    def runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
        calls.append(list(command))
        assert cwd == Path.cwd()
        assert env["COMPOSE_PROJECT_NAME"] == STAGING_PROJECT

    manager = StagingManager(staging, runner)
    manager.stop()

    assert calls == [staging.compose_command("stop")]
    assert "--volumes" not in calls[0]
    assert calls[0][calls[0].index("--project-name") + 1] == STAGING_PROJECT
    assert calls[0][calls[0].index("--file") + 1].endswith("compose.staging.yaml")


def test_start_prepares_private_roots_and_migrates_only_staging(
    tmp_path: Path,
) -> None:
    env_file = write_override(tmp_path)
    staging = StagingEnvironment.load(tmp_path, env_file)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
        calls.append((list(command), env))

    StagingManager(staging, runner).start()

    assert [call[0] for call in calls] == [
        staging.compose_command("up", "-d", "--wait"),
        staging.application_command("alembic", "upgrade", "head"),
    ]
    assert all(call[1]["POSTGRES_DB"] == "health_agent_staging" for call in calls)
    assert all("DATABASE_URL" not in call[1] for call in calls)
    assert stat.S_IMODE((tmp_path / ".staging").stat().st_mode) == 0o700
    for key in (
        "VAULT_ROOT",
        "TEMPORARY_ROOT",
        "WHOOP_TOKEN_ROOT",
        "CONNECTOR_STATE_ROOT",
        "GMAIL_ROOT",
        "TELEGRAM_ROOT",
    ):
        path = staging.resolve_path(staging.values[key])
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_clean_requires_exact_staging_confirmation_before_runner() -> None:
    staging = load_example()
    calls: list[list[str]] = []

    def runner(command: Sequence[str], cwd: Path, env: dict[str, str]) -> None:
        calls.append(list(command))

    manager = StagingManager(staging, runner)

    with pytest.raises(StagingConfigurationError, match="requires --confirm"):
        manager.clean("health-agent")
    assert calls == []

    manager.clean(STAGING_PROJECT)
    assert calls == [staging.compose_command("down", "--volumes", "--remove-orphans")]


def test_application_environment_drops_inherited_production_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = load_example()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://health_agent:secret@127.0.0.1:55432/health_agent",
    )
    monkeypatch.setenv("WHOOP_CLIENT_ID", "production-client")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "production-secret")

    environment = staging.subprocess_environment()

    assert "DATABASE_URL" not in environment
    assert "WHOOP_CLIENT_ID" not in environment
    assert "WHOOP_CLIENT_SECRET" not in environment
    assert environment["POSTGRES_PORT"] == "56432"
    assert environment["HEALTH_AGENT_ENV_FILE"].endswith(".env.staging.example")


def test_staging_compose_declares_fixed_separate_databases_and_volumes() -> None:
    production_compose = Path("compose.yaml").read_text(encoding="utf-8")
    compose = Path("compose.staging.yaml").read_text(encoding="utf-8")

    assert production_compose.startswith(f"name: {PRODUCTION_PROJECT}\n")
    assert "name: health-agent-staging" in compose
    assert f"name: {PRODUCTION_PROJECT}\n" not in compose
    assert "staging_health_postgres" in compose
    assert "staging_health_metabase" in compose
    assert "health_agent_staging" in compose
    assert "MB_DB_DBNAME: metabase_staging" in compose
    assert "STAGING_METABASE_DB" not in compose
    assert "55432" not in compose
    assert "53000" not in compose
