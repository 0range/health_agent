"""Persistent panel launch agent, reusing the existing managed lifecycle."""

import plistlib
from dataclasses import replace
from pathlib import Path

from health_agent.automation.launchd import LaunchdManager, LaunchdPaths

PANEL_LABEL = "com.orange.health-agent.panel"


def panel_launchd_paths(
    *,
    automation_root: Path,
    executable: Path,
    environment_file: Path,
    working_directory: Path,
    home: Path | None = None,
) -> LaunchdPaths:
    paths = LaunchdPaths.resolve(
        automation_root=automation_root,
        executable=executable,
        environment_file=environment_file,
        working_directory=working_directory,
        home=home,
    )
    return replace(
        paths,
        rendered_plist=paths.rendered_plist.with_name(f"{PANEL_LABEL}.plist"),
        installed_plist=paths.installed_plist.with_name(f"{PANEL_LABEL}.plist"),
        stdout_log=paths.stdout_log.with_name("panel-stdout.log"),
        stderr_log=paths.stderr_log.with_name("panel-stderr.log"),
        state_file=paths.state_file.with_name("panel-state.json"),
        lock_file=paths.lock_file.with_name("panel.lock"),
    )


class PanelLaunchdManager(LaunchdManager):
    @property
    def service(self) -> str:
        return f"{self.domain}/{PANEL_LABEL}"

    def _plist_bytes(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": PANEL_LABEL,
                "ProgramArguments": [str(self.paths.executable), "panel", "serve"],
                "EnvironmentVariables": {
                    "HEALTH_AGENT_ENV_FILE": str(self.paths.environment_file)
                },
                "WorkingDirectory": str(self.paths.working_directory),
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 30,
                "ProcessType": "Background",
                "StandardOutPath": str(self.paths.stdout_log),
                "StandardErrorPath": str(self.paths.stderr_log),
            },
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
