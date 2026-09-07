"""Local lifecycle commands for the three pilot journeys."""

import getpass
import json
import os
import plistlib
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

import typer

from health_agent.automation.storage import atomic_private_write, private_directory
from health_agent.config import Settings
from health_agent.db import build_engine
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.runtime import HELP, _pilot_lock, run_pilot, telegram_root
from health_agent.pilot.storage import PilotStore
from health_agent.telegram.admin import DatabaseProfileDirectory, TelegramAdminService
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState

app = typer.Typer(help="Run the sleep, food and training pilot bots.")


@app.command("apple-import")
def apple_import(
    export_file: Path,
    profile_id: Annotated[UUID, typer.Option("--profile-id")] = DEFAULT_PROFILE_ID,
) -> None:
    from health_agent.pilot.apple_import import import_apple_export

    store = PilotStore(build_engine(Settings()))
    try:
        counts = import_apple_export(store, profile_id, export_file)
        typer.echo(json.dumps(counts))
    finally:
        store.engine.dispose()


@app.command("coros-sync")
def coros_sync(
    since: Annotated[str, typer.Option("--since")] = "2010-01-01",
    until: Annotated[str | None, typer.Option("--until")] = None,
    profile_id: Annotated[UUID, typer.Option("--profile-id")] = DEFAULT_PROFILE_ID,
) -> None:
    """Preserve raw COROS responses and parsed activity history on this Mac."""
    from health_agent.pilot.coros_auth import CorosOAuth
    from health_agent.pilot.coros_sync import run_coros_sync

    store = PilotStore(build_engine(Settings()))
    root = Path("data/pilot/training/coros") / str(profile_id)
    try:
        with _pilot_lock(root / "sync.lock"):
            counts = run_coros_sync(
                store,
                CorosOAuth(root),
                profile_id,
                root,
                date.fromisoformat(since),
                date.fromisoformat(until)
                if until
                else datetime.now(ZoneInfo("Europe/Moscow")).date(),
            )
        typer.echo(json.dumps(counts))
        if counts["incomplete"]:
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except Exception as error:  # noqa: BLE001 -- no provider payloads or secrets in logs
        typer.echo(f"COROS sync failed: {type(error).__name__}", err=True)
        raise typer.Exit(1) from None
    finally:
        store.engine.dispose()


@app.command("coros-connect")
def coros_connect(profile_id: UUID = DEFAULT_PROFILE_ID) -> None:
    from health_agent.pilot.coros_connect import connect

    try:
        ready = connect(Path("data/pilot/training/coros") / str(profile_id))
    except Exception as error:  # noqa: BLE001 -- callback and provider secrets stay private
        typer.echo(f"COROS connection failed: {type(error).__name__}")
        raise typer.Exit(1) from None
    typer.echo("COROS connected" if ready else "COROS consent timed out")
    if not ready:
        raise typer.Exit(1)


@app.command("run")
def run(
    domain: str,
    env_file: Annotated[Path, typer.Option("--env-file")] = Path(".env"),
    profile_id: Annotated[UUID, typer.Option("--profile-id")] = DEFAULT_PROFILE_ID,
) -> None:
    try:
        run_pilot(Settings(_env_file=env_file), domain, profile_id)  # type: ignore[call-arg]
    except KeyboardInterrupt:
        return
    except Exception as error:  # noqa: BLE001 -- no private exception content in logs
        typer.echo(
            f"pilot={domain} status=failed error={type(error).__name__}", err=True
        )
        raise typer.Exit(1) from None


@app.command("status")
def status(profile_id: UUID = DEFAULT_PROFILE_ID) -> None:
    settings = Settings()
    store = PilotStore(build_engine(settings))
    for domain in HELP:
        root = telegram_root(settings, domain)
        try:
            token_path = (
                settings.effective_telegram_token_file
                if domain == "sleep"
                else root / "bot-token"
            )
            credential = PrivateBotTokenStore(token_path).load_verified()
            state = SqliteTelegramState(
                settings.telegram_state_file
                if domain == "sleep"
                else root / "state.sqlite3"
            )
            bound = (
                state.identity_for_profile(credential.bot_id, profile_id) is not None
            )
            count = len(store.list(profile_id, domain, limit=1000))
            _, polled_at, error = state.runtime_status(credential.bot_id)
            recent = (
                polled_at is not None
                and (datetime.now(UTC) - polled_at).total_seconds() < 120
            )
            typer.echo(
                f"{domain}: bot=@{credential.username} bound={bound} records={count} recent_poll={recent} error={error}"
            )
        except Exception as error:  # noqa: BLE001
            typer.echo(f"{domain}: unavailable={type(error).__name__}")
    store.engine.dispose()
    from health_agent.pilot.coros_auth import CorosOAuth

    typer.echo(
        f"coros: authorized={CorosOAuth(Path('data/pilot/training/coros') / str(profile_id)).status()}"
    )


@app.command("configure")
def configure(domain: str, user_id: int, profile_id: UUID = DEFAULT_PROFILE_ID) -> None:
    if domain not in {"food", "training"}:
        raise typer.BadParameter("Use food or training; sleep uses the existing bot.")
    settings = Settings()
    root = telegram_root(settings, domain)
    admin = TelegramAdminService(
        PrivateBotTokenStore(root / "bot-token"),
        SqliteTelegramState(root / "state.sqlite3"),
        DatabaseProfileDirectory(settings),
    )
    credential = admin.configure_token(getpass.getpass("Telegram token (hidden): "))
    admin.bind_identity(profile_id, user_id, user_id)
    typer.echo(f"Configured @{credential.username}; press Start in that bot.")


def launchd_payload(domain: str, repo: Path, env_file: Path, profile_id: UUID) -> dict:
    if domain not in {"food", "training"}:
        raise ValueError("sleep_uses_existing_telegram_service")
    root = repo / "data" / "pilot" / domain
    return {
        "Label": f"com.orange.health-agent.pilot.{domain}",
        "ProgramArguments": [
            str(repo / ".venv/bin/health-agent"),
            "pilot",
            "run",
            domain,
            "--env-file",
            str(env_file),
            "--profile-id",
            str(profile_id),
        ],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "StandardOutPath": str(root / "stdout.log"),
        "StandardErrorPath": str(root / "stderr.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
    }


@app.command("install")
def install(
    domain: str,
    env_file: Annotated[Path, typer.Option("--env-file")] = Path(".env"),
    profile_id: Annotated[UUID, typer.Option("--profile-id")] = DEFAULT_PROFILE_ID,
) -> None:
    repo = Path.cwd().resolve()
    environment = env_file.resolve()
    if not environment.is_file():
        raise typer.BadParameter("Environment file missing")
    payload = launchd_payload(domain, repo, environment, profile_id)
    settings = Settings(_env_file=environment)  # type: ignore[call-arg]
    PrivateBotTokenStore(telegram_root(settings, domain) / "bot-token").load_verified()
    root = private_directory(repo / "data" / "pilot" / domain)
    for filename in ("stdout.log", "stderr.log"):
        path = root / filename
        if not path.exists():
            atomic_private_write(path, b"")
        path.chmod(0o600)
    target = Path.home() / "Library" / "LaunchAgents" / f"{payload['Label']}.plist"
    atomic_private_write(target, plistlib.dumps(payload))
    gui = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{gui}/{payload['Label']}"],
        capture_output=True,
        check=False,
    )
    completed = subprocess.run(
        ["launchctl", "bootstrap", gui, str(target)], capture_output=True, check=False
    )
    if completed.returncode:
        raise typer.Exit(1)
    typer.echo(f"Installed {domain} pilot service")


@app.command("load-private")
def load_private(path: Path, profile_id: UUID = DEFAULT_PROFILE_ID) -> None:
    """Load explicitly provided local goals/protocol, never from tracked fixtures."""
    values = json.loads(path.read_text())
    if not isinstance(values, list):
        raise typer.BadParameter("Expected a list of records")
    store = PilotStore(build_engine(Settings()))
    aliases: dict[str, str] = {}
    try:
        for value in values:
            payload = dict(value["payload"])
            parent_alias = payload.pop("parent_alias", None)
            if parent_alias is not None:
                payload["parent_id"] = aliases[parent_alias]
            record = store.put(
                profile_id, value["domain"], value["kind"], value["source_key"], payload
            )
            aliases[value["source_key"]] = record.id
        typer.echo(f"Loaded {len(values)} private records")
    finally:
        store.engine.dispose()
