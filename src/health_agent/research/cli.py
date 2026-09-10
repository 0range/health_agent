"""Independent quality checks continue even if a source cannot authenticate."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from health_agent.automation.launchd import (
    LaunchdManager,
    LaunchdPaths,
    rotate_safe_logs,
)
from health_agent.automation.storage import GlobalRunLock, atomic_private_write
from health_agent.config import Settings
from health_agent.db import build_engine
from health_agent.qingping.service import Connection
from health_agent.research.dataset import export_day
from health_agent.research.participants import check_participants

app = typer.Typer(help="Private room/sleep research quality reports.")
LABEL = "com.orange.health-agent.research-quality"


@app.command("export")
def export_dataset(day: Annotated[str, typer.Option("--day")]) -> None:
    """Export one Moscow study date; current-day exports are explicitly partial."""
    try:
        selected = date.fromisoformat(day)
    except ValueError:
        raise typer.BadParameter("Use YYYY-MM-DD") from None
    settings = Settings()
    lock = GlobalRunLock(settings.research_root / "quality.lock")
    if not lock.acquire():
        typer.echo("status=skipped reason=already_running")
        return
    try:
        result = export_day(
            settings,
            build_engine(settings),
            Connection.load(settings.qingping_connection_file),
            selected,
        )
        typer.echo(
            f"status=exported date={day} rows={result['rows']} participants={len(result['participants'])} partial_day={result['partial_day']}"
        )
    except Exception:  # noqa: BLE001 - never expose raw source or connection configuration
        typer.echo("status=failed safe_error=research_export_failed", err=True)
        raise typer.Exit(1) from None
    finally:
        lock.release()


class QualityLaunchdManager(LaunchdManager):
    @property
    def service(self) -> str:
        return f"{self.domain}/{LABEL}"

    def _plist_bytes(self) -> bytes:
        data = plistlib.loads(super()._plist_bytes())
        data.update(
            Label=LABEL,
            ProgramArguments=[str(self.paths.executable), "research", "check"],
            StartInterval=3600,
            StartCalendarInterval={"Hour": 10, "Minute": 0},
            EnvironmentVariables={
                "HEALTH_AGENT_ENV_FILE": str(self.paths.environment_file),
                "PATH": str(
                    Path(shutil.which("docker") or "/opt/homebrew/bin/docker").parent
                )
                + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            },
        )
        return plistlib.dumps(data)


@app.command("check")
def check() -> None:
    settings = Settings()
    lock = GlobalRunLock(settings.research_root / "quality.lock")
    if not lock.acquire():
        typer.echo("status=skipped reason=already_running")
        return
    try:
        result = check_participants(
            settings,
            build_engine(settings),
            Connection.load(settings.qingping_connection_file),
        )
        typer.echo(
            f"status={result['status']} daily_reports={len(result['daily_reports'])}"
        )
        if result["status"] != "ok":
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception:  # noqa: BLE001 - never print private paths, DB URLs or raw data
        atomic_private_write(
            settings.research_root / "status.json",
            json.dumps(
                {
                    "status": "attention",
                    "safe_error": "research_check_failed",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            ).encode(),
        )
        typer.echo("status=attention safe_error=research_check_failed", err=True)
        raise typer.Exit(1) from None
    finally:
        lock.release()


@app.command("status")
def status() -> None:
    path = Settings().research_root / "status.json"
    if not path.exists():
        typer.echo("status=not_checked")
        raise typer.Exit(1)
    result = json.loads(path.read_text())
    # Make a stopped checker visible, even if its last recorded result was green.
    at = datetime.fromisoformat(result["checked_at"])
    if (datetime.now(UTC) - at).total_seconds() > 7200:
        result.update(status="attention", checker_stale=True)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("install")
def install(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    os.environ["HEALTH_AGENT_ENV_FILE"] = str(env_file.absolute())
    settings = Settings()
    Connection.load(settings.qingping_connection_file)
    paths = LaunchdPaths.resolve(
        automation_root=settings.research_root,
        environment_file=env_file.absolute(),
        executable=Path(sys.executable).parent / "health-agent",
        working_directory=Path.cwd(),
    )
    paths = replace(
        paths,
        rendered_plist=paths.rendered_plist.with_name(LABEL + ".plist"),
        installed_plist=paths.installed_plist.with_name(LABEL + ".plist"),
    )
    rotate_safe_logs(paths)
    result = QualityLaunchdManager(paths).install()
    typer.echo(f"status={result} interval_seconds=3600 daily_local_time=10:00")
