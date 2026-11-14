"""Install the independent local race-monitor LaunchAgent."""

import os
import plistlib
import subprocess
from pathlib import Path

from health_agent.automation.storage import atomic_private_write, private_directory

LABEL = "com.orange.health-agent.race-monitor"


def payload(repo: Path) -> dict:
    logs = repo / "data/race-monitor/logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(repo / ".venv/bin/python"),
            "-m",
            "health_agent.races.monitor",
            "--scheduled",
        ],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "StartInterval": 60,
        "StartCalendarInterval": {"Minute": 0},
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
    }


def main() -> None:
    repo = Path.cwd().resolve()
    if not (repo / "races-2027.json").exists():
        raise SystemExit("Run from the Health Agent repository")
    private_directory(repo / "data/race-monitor/logs")
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if plist.exists():
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LABEL}"],
            capture_output=True,
            check=False,
        )
    atomic_private_write(plist, plistlib.dumps(payload(repo)))
    subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    print(f"Installed {LABEL}: 13:00 Europe/Moscow, catch-up after wake/login")


if __name__ == "__main__":
    main()
