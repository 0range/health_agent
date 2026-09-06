import plistlib

import pytest
from typer.testing import CliRunner

from health_agent import cli
from health_agent.automation.launchd import LaunchdError
from health_agent.panel.launchd import (
    PANEL_LABEL,
    PanelLaunchdManager,
    panel_launchd_paths,
)


class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.calls = []

    def run(self, arguments):
        self.calls.append(arguments)
        if arguments[0] == "print":
            return 0 if self.loaded else 113
        self.loaded = arguments[0] == "bootstrap"
        return 0


def paths(tmp_path):
    executable = tmp_path / "health-agent"
    executable.write_text("synthetic executable")
    executable.chmod(0o700)
    env = tmp_path / "private.env"
    env.write_text("SECRET_VALUE=NEVER_IN_PLIST")
    env.chmod(0o600)
    return panel_launchd_paths(
        automation_root=tmp_path / "automation",
        executable=executable,
        environment_file=env,
        working_directory=tmp_path,
        home=tmp_path / "owner",
    )


def test_panel_payload_is_private_and_separate(tmp_path):
    resolved = paths(tmp_path)
    manager = PanelLaunchdManager(resolved, platform="darwin", uid=501)
    rendered = manager.render()
    payload = plistlib.loads(rendered.read_bytes())
    assert payload["Label"] == PANEL_LABEL == "com.orange.health-agent.panel"
    assert payload["ProgramArguments"] == [str(resolved.executable), "panel", "serve"]
    assert payload["EnvironmentVariables"] == {
        "HEALTH_AGENT_ENV_FILE": str(resolved.environment_file)
    }
    assert payload["KeepAlive"] is True and payload["RunAtLoad"] is True
    assert payload["ThrottleInterval"] == 30 and "StartInterval" not in payload
    assert payload["StandardOutPath"].endswith("panel-stdout.log")
    assert payload["StandardErrorPath"].endswith("panel-stderr.log")
    assert b"NEVER_IN_PLIST" not in rendered.read_bytes()
    assert rendered.stat().st_mode & 0o777 == 0o600
    assert resolved.installed_plist.name == f"{PANEL_LABEL}.plist"


def test_lifecycle_only_targets_panel_and_stop_preserves_files(tmp_path):
    resolved = paths(tmp_path)
    fake = FakeLaunchctl()
    manager = PanelLaunchdManager(resolved, launchctl=fake, platform="darwin", uid=501)
    assert manager.install() == "installed"
    assert manager.install() == "installed"
    assert sum(call[0] == "bootstrap" for call in fake.calls) == 1
    assert manager.status() == "loaded"
    assert manager.stop() == "stopped"
    assert resolved.installed_plist.exists()
    assert all(
        call[1] == f"gui/501/{PANEL_LABEL}"
        for call in fake.calls
        if call[0] in {"print", "bootout"}
    )


def test_environment_must_be_private_and_lifecycle_requires_macos(tmp_path):
    resolved = paths(tmp_path)
    with pytest.raises(LaunchdError, match="macos"):
        PanelLaunchdManager(resolved, platform="linux").install()
    resolved.environment_file.chmod(0o644)
    with pytest.raises((ValueError, RuntimeError)):
        panel_launchd_paths(
            automation_root=resolved.automation_root,
            executable=resolved.executable,
            environment_file=resolved.environment_file,
            working_directory=resolved.working_directory,
            home=tmp_path / "owner",
        )


@pytest.mark.parametrize("action", ["install", "status", "stop"])
def test_cli_dispatch_and_safe_failure(monkeypatch, action):
    class Manager:
        def install(self):
            return "installed"

        def status(self):
            return "loaded"

        def stop(self):
            return "stopped"

    monkeypatch.setattr(cli, "_panel_launchd_manager", lambda path: Manager())
    result = CliRunner().invoke(cli.app, ["panel", action, "--env-file", "private.env"])
    assert result.exit_code == 0 and PANEL_LABEL in result.output

    def failed(path):
        raise RuntimeError("PRIVATE_DIAGNOSTIC")

    monkeypatch.setattr(cli, "_panel_launchd_manager", failed)
    result = CliRunner().invoke(cli.app, ["panel", action, "--env-file", "private.env"])
    assert result.exit_code == 1 and "PRIVATE_DIAGNOSTIC" not in result.output
