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
