from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from health_agent.cli import app


def test_configure_and_status_are_profile_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(tmp_path / "drive"))
    runner = CliRunner()
    root = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"

    configured = runner.invoke(app, ["drive", "configure", "alice", root])
    status = runner.invoke(app, ["drive", "status", "alice"])

    assert configured.exit_code == 0
    assert "status=configured profile=alice roots=1" in configured.stdout
    assert status.exit_code == 0
    assert "profile=alice" in status.stdout
    assert "authorized=no" in status.stdout
    assert "cursor=none" in status.stdout
    assert "token" not in status.stdout.casefold()
