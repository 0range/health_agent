import plistlib

from health_agent.automation.launchd import LABEL as GENERAL_LABEL
from health_agent.automation.launchd import LaunchdManager
from health_agent.qingping.launchd import LABEL, QingpingLaunchdManager, sensor_paths


def test_minute_schedule_uses_own_label_and_explicit_environment(tmp_path):
    executable = tmp_path / "health-agent"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    env = tmp_path / ".env"
    env.write_text("SYNTHETIC=true\n")
    env.chmod(0o600)
    paths = sensor_paths(tmp_path / "qingping", env, executable, tmp_path, tmp_path)
    manager = QingpingLaunchdManager(paths, platform="darwin", uid=501)
    payload = plistlib.loads(manager._plist_bytes())
    assert payload["Label"] == LABEL
    assert payload["StartInterval"] == 60
    assert payload["ProgramArguments"] == [str(executable), "qingping", "sync"]
    assert payload["EnvironmentVariables"] == {"HEALTH_AGENT_ENV_FILE": str(env)}
    assert paths.installed_plist.name == LABEL + ".plist"
    assert paths.rendered_plist.name == LABEL + ".plist"
    assert manager.service == "gui/501/" + LABEL
    original = plistlib.loads(LaunchdManager(paths)._plist_bytes())
    assert original["Label"] == GENERAL_LABEL
    assert original["StartInterval"] == 14400
