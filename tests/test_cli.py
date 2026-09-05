from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine
from typer.testing import CliRunner

from health_agent import cli
from health_agent.db import session_scope
from health_agent.models import Profile


def test_help_is_available() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "Personal Health Agent" in result.stdout


def test_profile_list_survives_real_session_commit_and_close(
    clean_database: Engine,
    monkeypatch,
) -> None:
    profile_id = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=profile_id, name="Second profile"))
    monkeypatch.setenv(
        "DATABASE_URL", clean_database.url.render_as_string(hide_password=False)
    )

    result = CliRunner().invoke(cli.app, ["profile", "list"])

    assert result.exit_code == 0
    assert f"profile_id={profile_id} name=Second profile" in result.stdout


def test_compose_has_a_clean_checkout_password_default() -> None:
    compose = Path("compose.yaml").read_text()

    assert compose.count("${POSTGRES_PASSWORD:-health_agent}") == 2


def test_review_correct_cli_passes_explicit_values(clean_database, monkeypatch):
    from types import SimpleNamespace

    calls = []
    item_id, corrected_id = uuid4(), uuid4()
    monkeypatch.setattr(cli, "build_engine", lambda _: clean_database)

    def correct(_session, observation_id, **kwargs):
        calls.append((observation_id, kwargs))
        return SimpleNamespace(id=corrected_id)

    monkeypatch.setattr(cli, "correct_observation", correct, raising=False)
    result = CliRunner().invoke(
        cli.app,
        [
            "review",
            "correct",
            str(item_id),
            "--value",
            "42,5",
            "--unit",
            "ng/mL",
            "--profile-id",
            "00000000-0000-0000-0000-000000000001",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert f"corrected_observation_id={corrected_id}" in result.stdout
    assert calls[0][0] == item_id
    assert calls[0][1]["source_value"] == "42,5"
    assert calls[0][1]["source_unit"] == "ng/mL"


def test_review_correct_cli_failure_has_no_private_details(clean_database, monkeypatch):
    monkeypatch.setattr(cli, "build_engine", lambda _: clean_database)

    def fail(*_args, **_kwargs):
        raise ValueError("private token and source text")

    monkeypatch.setattr(cli, "correct_observation", fail, raising=False)
    result = CliRunner().invoke(
        cli.app,
        [
            "review",
            "correct",
            str(uuid4()),
            "--value",
            "42",
            "--unit",
            "ng/mL",
            "--profile-id",
            "00000000-0000-0000-0000-000000000001",
        ],
    )
    assert result.exit_code == 1
    assert "not applied" in result.stdout
    assert "private" not in result.stdout
