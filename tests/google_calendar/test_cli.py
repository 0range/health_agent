from types import SimpleNamespace
from uuid import uuid4

from typer.testing import CliRunner

from health_agent.google_calendar.cli import create_cli


def test_cli_requires_profile_option_and_redacts_failures():
    profile_id = uuid4()
    broken = SimpleNamespace(
        profiles=SimpleNamespace(
            load=lambda _: (_ for _ in ()).throw(RuntimeError("secret-token"))
        ),
        oauth=SimpleNamespace(),
    )
    app = create_cli(lambda: broken, profile_validator=lambda _: None)
    missing = CliRunner().invoke(app, ["status", str(profile_id)])
    assert missing.exit_code != 0 and "--profile-id" in missing.output
    failed = CliRunner().invoke(app, ["status", "--profile-id", str(profile_id)])
    assert failed.exit_code == 1
    assert "calendar_status_unavailable" in failed.output
    assert "secret-token" not in failed.output
