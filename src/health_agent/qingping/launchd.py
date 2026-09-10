"""Minute collection independent of the general four-hour connector schedule."""

import plistlib
from dataclasses import replace
from pathlib import Path

from health_agent.automation.launchd import LaunchdManager, LaunchdPaths

LABEL = "com.orange.health-agent.qingping"


class QingpingLaunchdManager(LaunchdManager):
    @property
    def service(self) -> str:
        return f"{self.domain}/{LABEL}"

    def _plist_bytes(self) -> bytes:
        payload = plistlib.loads(super()._plist_bytes())
        payload.update(
            Label=LABEL,
            ProgramArguments=[str(self.paths.executable), "qingping", "sync"],
            EnvironmentVariables={
                "HEALTH_AGENT_ENV_FILE": str(self.paths.environment_file)
            },
            StartInterval=60,
        )
        return plistlib.dumps(payload)


def sensor_paths(
    root: Path,
    env_file: Path,
    executable: Path,
    working: Path,
    home: Path | None = None,
) -> LaunchdPaths:
    paths = LaunchdPaths.resolve(
        automation_root=root,
        environment_file=env_file,
        executable=executable,
        working_directory=working,
        home=home,
    )
    return replace(
        paths,
        rendered_plist=paths.rendered_plist.with_name(LABEL + ".plist"),
        installed_plist=paths.installed_plist.with_name(LABEL + ".plist"),
    )
