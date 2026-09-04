from __future__ import annotations

import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from health_agent.staging import (
    PRODUCTION_METABASE_PORT,
    PRODUCTION_POSTGRES_PORT,
    STAGING_PROJECT,
    StagingConfigurationError,
    StagingEnvironment,
    StagingManager,
)


def load_example() -> StagingEnvironment:
    return StagingEnvironment.load(Path.cwd(), Path(".env.staging.example"))


def write_override(tmp_path: Path, **overrides: str) -> Path:
    values: dict[str, str] = {}
    for raw_line in (
        Path(".env.staging.example").read_text(encoding="utf-8").splitlines()
    ):
        line = raw_line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", maxsplit=1)
            values[key] = value
    values.update(overrides)
    target = tmp_path / ".env.staging"
    target.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return target


def test_example_is_separate_from_production_targets() -> None:
    staging = load_example()

    assert staging.values["STAGING_COMPOSE_PROJECT"] == STAGING_PROJECT
    assert int(staging.values["POSTGRES_PORT"]) != PRODUCTION_POSTGRES_PORT
    assert int(staging.values["STAGING_METABASE_PORT"]) != PRODUCTION_METABASE_PORT
    assert staging.values["POSTGRES_DB"] != "health_agent"
    assert staging.values["STAGING_METABASE_DB"] != "metabase"
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
        ("STAGING_METABASE_DB", "metabase", "production Metabase"),
        ("POSTGRES_PASSWORD", "no-digits", "six characters and a digit"),
        ("VAULT_ROOT", "data/vault", "under .staging"),
        (
            "WHOOP_CLIENT_CREDENTIALS_FILE",
            ".tokens/whoop-client.json",
            "separate .staging file",
        ),
    ),
)
def test_production_overlap_fails_closed(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    env_file = write_override(tmp_path, **{key: value})

    with pytest.raises(StagingConfigurationError, match=message):
        StagingEnvironment.load(Path.cwd(), env_file)


def test_database_url_must_match_staging_database_and_port(tmp_path: Path) -> None:
    env_file = write_override(
        tmp_path,
        DATABASE_URL=(
            "postgresql+psycopg://health_agent:health_agent@127.0.0.1:55432/"
            "health_agent"
        ),
    )

    with pytest.raises(StagingConfigurationError, match="does not match"):
        StagingEnvironment.load(Path.cwd(), env_file)


def test_staging_cannot_load_production_env() -> None:
    with pytest.raises(StagingConfigurationError, match="production .env"):
        StagingEnvironment.load(Path.cwd(), Path(".env"))


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
        "TEMP_ROOT",
        "WHOOP_TOKEN_ROOT",
        "CONNECTOR_STATE_ROOT",
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
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://health_agent:secret@127.0.0.1:55432/health_agent",
    )
    monkeypatch.setenv("WHOOP_CLIENT_ID", "production-client")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "production-secret")
    staging = load_example()

    environment = staging.subprocess_environment()

    assert "DATABASE_URL" not in environment
    assert "WHOOP_CLIENT_ID" not in environment
    assert "WHOOP_CLIENT_SECRET" not in environment
    assert environment["POSTGRES_PORT"] == "56432"
    assert environment["HEALTH_AGENT_ENV_FILE"].endswith(".env.staging.example")


def test_staging_compose_declares_separate_databases_and_volumes() -> None:
    compose = Path("compose.staging.yaml").read_text(encoding="utf-8")

    assert "name: health-agent-staging" in compose
    assert "staging_health_postgres" in compose
    assert "staging_health_metabase" in compose
    assert "health_agent_staging" in compose
    assert "metabase_staging" in compose
    assert "55432" not in compose
    assert "53000" not in compose
