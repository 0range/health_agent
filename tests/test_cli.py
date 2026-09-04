from pathlib import Path

from typer.testing import CliRunner

from health_agent.cli import app


def test_help_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Personal Health Agent" in result.stdout


def test_compose_has_a_clean_checkout_password_default() -> None:
    compose = Path("compose.yaml").read_text()

    assert compose.count("${POSTGRES_PASSWORD:-health_agent}") == 2
