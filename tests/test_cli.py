from typer.testing import CliRunner

from health_agent.cli import app


def test_help_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Personal Health Agent" in result.stdout
