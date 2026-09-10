"""Hourly archival of owner-authorized WHOOP physiological details."""

from __future__ import annotations

import json
import os
import plistlib
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer

from health_agent.automation.launchd import LaunchdManager, LaunchdPaths
from health_agent.automation.storage import GlobalRunLock, atomic_private_write
from health_agent.config import Settings
from health_agent.db import build_engine
from health_agent.pilot.storage import PilotStore
from health_agent.whoop.details import DetailClient, DetailError, DetailService, claims

app = typer.Typer(help="WHOOP app physiological detail archive.")
LABEL = "com.orange.health-agent.whoop-detail"


class DetailLaunchdManager(LaunchdManager):
    @property
    def service(self) -> str:
        return f"{self.domain}/{LABEL}"

    def _plist_bytes(self) -> bytes:
        data = plistlib.loads(super()._plist_bytes())
        data.update(
            Label=LABEL,
            ProgramArguments=[str(self.paths.executable), "whoop-detail", "sync"],
            StartInterval=3600,
            EnvironmentVariables={
                "HEALTH_AGENT_ENV_FILE": str(self.paths.environment_file)
            },
        )
        return plistlib.dumps(data)


@contextmanager
def operation():
    settings = Settings()
    lock = GlobalRunLock(settings.whoop_detail_root / "sync.lock")
    acquired = False
    try:
        acquired = lock.acquire()
        if not acquired:
            typer.echo("status=skipped reason=already_running")
            raise typer.Exit()
        with httpx.Client(timeout=30) as http:
            yield settings, DetailClient(http, settings.whoop_detail_session_file)
    except typer.Exit:
        raise
    except Exception as error:  # noqa: BLE001 - never expose upstream bodies/tokens at the CLI boundary
        code = (
            str(error)
            if isinstance(error, DetailError)
            else "whoop_detail_operation_failed"
        )
        atomic_private_write(
            settings.whoop_detail_root / "last_error.json",
            json.dumps(
                {
                    "error": code,
                    "at": datetime.now(UTC).isoformat(),
                }
            ).encode(),
        )
        typer.echo(f"status=failed safe_error={code}", err=True)
        raise typer.Exit(1) from None
    finally:
        if acquired:
            lock.release()


@app.command("sync")
def sync() -> None:
    with operation() as (settings, client):
        report = DetailService(
            PilotStore(build_engine(settings)), settings.whoop_detail_root
        ).sync(client)
        typer.echo(
            f"status={report['status']} resources={report['resources']} errors={len(report['errors'])}"
        )
        if report["errors"]:
            raise typer.Exit(1)
        atomic_private_write(settings.whoop_detail_root / "last_error.json", b"{}")


@app.command("refresh")
def refresh() -> None:
    with operation() as (_, client):
        client.refresh()
        client.verify()
        typer.echo("status=refreshed identity=verified")


@app.command("status")
def status() -> None:
    with operation() as (settings, client):
        state_path = settings.whoop_detail_root / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        error_path = settings.whoop_detail_root / "last_error.json"
        error = json.loads(error_path.read_text()) if error_path.exists() else {}
        state.update(
            session_expires_at=datetime.fromtimestamp(
                claims(client.token)["exp"], UTC
            ).isoformat(),
            last_error=error,
        )
        typer.echo(json.dumps(state, ensure_ascii=False, indent=2))


@app.command("install")
def install(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    os.environ["HEALTH_AGENT_ENV_FILE"] = str(env_file.absolute())
    with operation() as (settings, client):
        client.verify()
        paths = LaunchdPaths.resolve(
            automation_root=settings.whoop_detail_root,
            environment_file=env_file.absolute(),
            executable=Path(sys.executable).parent / "health-agent",
            working_directory=Path.cwd(),
        )
        paths = replace(
            paths,
            rendered_plist=paths.rendered_plist.with_name(LABEL + ".plist"),
            installed_plist=paths.installed_plist.with_name(LABEL + ".plist"),
        )
        result = DetailLaunchdManager(paths).install()
        typer.echo(f"status={result} interval_seconds=3600")
