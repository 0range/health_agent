"""Configure and inspect one explicitly selected Qingping device."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
import typer
from sqlalchemy import select

from health_agent.automation.launchd import rotate_safe_logs
from health_agent.automation.storage import GlobalRunLock
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.models import Profile
from health_agent.pilot.storage import PilotStore
from health_agent.qingping.client import QingpingClient, QingpingError
from health_agent.qingping.launchd import QingpingLaunchdManager, sensor_paths
from health_agent.qingping.service import (
    Connection,
    Placement,
    QingpingService,
    credentials,
)

app = typer.Typer(help="Qingping room measurements.")


@contextmanager
def operation():
    settings = Settings()
    lock = GlobalRunLock(settings.qingping_root / "sync.lock")
    acquired = False
    try:
        acquired = lock.acquire()
        if not acquired:
            typer.echo("status=skipped reason=already_running")
            raise typer.Exit()
        yield settings
    except typer.Exit:
        raise
    except Exception as error:  # noqa: BLE001 - CLI must never print credential-bearing exceptions
        code = (
            str(error)
            if isinstance(error, QingpingError)
            else "qingping_operation_failed"
        )
        typer.echo(f"status=failed safe_error={code}", err=True)
        raise typer.Exit(1) from None
    finally:
        if acquired:
            lock.release()


def service(settings: Settings) -> QingpingService:
    return QingpingService(
        PilotStore(build_engine(settings)),
        Connection.load(settings.qingping_connection_file),
        settings.qingping_root / "state.json",
    )


@app.command("connect")
def connect(
    profile_id: Annotated[UUID, typer.Option("--profile-id")],
    room: Annotated[str, typer.Option("--room")],
    mac: Annotated[str | None, typer.Option("--mac")] = None,
) -> None:
    with operation() as settings:
        if settings.qingping_connection_file.exists():
            raise QingpingError("qingping_already_configured")
        with session_scope(build_engine(settings)) as session:
            if (
                session.scalar(select(Profile.id).where(Profile.id == profile_id))
                is None
            ):
                raise QingpingError("qingping_profile_not_found")
        with httpx.Client(timeout=30) as http:
            devices = QingpingClient(
                http, *credentials(settings.qingping_client_file)
            ).devices()
        if mac is not None:
            devices = [
                d
                for d in devices
                if d.get("info", {}).get("mac", "").upper() == mac.upper()
            ]
        if len(devices) != 1:
            raise QingpingError("qingping_select_one_device")
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        connection = Connection(
            profile_id=profile_id,
            mac=devices[0]["info"]["mac"].upper(),
            start_at=start,
            placements=[Placement(since=start, room=room.strip())],
        )
        connection.save(settings.qingping_connection_file)
        typer.echo("status=connected")


@app.command("sync")
def sync() -> None:
    with operation() as settings:
        # The daemon logs counts only; detailed status is an explicit local command.
        with httpx.Client(timeout=30) as http:
            result = service(settings).sync(
                QingpingClient(
                    http,
                    *credentials(settings.qingping_client_file),
                    token_path=settings.qingping_root / "token.json",
                )
            )
        typer.echo(
            f"status=synced samples={result['samples']} stale={str(result['stale']).lower()}"
        )


@app.command("status")
def status() -> None:
    with operation() as settings:
        typer.echo(json.dumps(service(settings).status(), ensure_ascii=False, indent=2))


@app.command("move")
def move(
    room: Annotated[str, typer.Option("--room")],
    at: Annotated[str | None, typer.Option("--at")] = None,
) -> None:
    """Record a room change, optionally with an explicit timezone-aware timestamp."""
    with operation() as settings:
        svc = service(settings)
        since = datetime.fromisoformat(at) if at else datetime.now(UTC)
        if (
            since.tzinfo is None
            or since > datetime.now(UTC)
            or since <= svc.connection.placements[-1].since
        ):
            raise QingpingError("qingping_invalid_move_time")
        updated = Connection.model_validate(
            {
                **svc.connection.model_dump(),
                "placements": [
                    *svc.connection.placements,
                    Placement(since=since, room=room.strip()),
                ],
            }
        )
        updated.save(settings.qingping_connection_file)
        svc.connection = updated
        svc.reconcile_rooms()
        typer.echo("status=moved")


@app.command("install")
def install(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    os.environ["HEALTH_AGENT_ENV_FILE"] = str(env_file.absolute())
    with operation() as settings:
        Connection.load(settings.qingping_connection_file)
        credentials(settings.qingping_client_file)
        paths = sensor_paths(
            settings.qingping_root,
            env_file.absolute(),
            Path(sys.executable).parent / "health-agent",
            Path.cwd(),
        )
        manager = QingpingLaunchdManager(paths)
        rotate_safe_logs(paths)
        result = manager.install()
        typer.echo(f"status={result} interval_seconds=60")
