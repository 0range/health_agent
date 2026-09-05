"""Production discovery adapters for configured connector identities."""

from __future__ import annotations

import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from health_agent.automation.models import AutomationJob, AutomationSource
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.gmail.stores import LocalGmailProfileStore
from health_agent.google_drive.stores import LocalProfileStore
from health_agent.google_sheets.stores import LocalSheetsProfileStore
from health_agent.whoop.models import WhoopConnection


class JobAdapter(Protocol):
    @property
    def source(self) -> AutomationSource: ...

    def discover(self, settings: Settings) -> Iterable[AutomationJob]: ...


@dataclass(frozen=True, slots=True)
class WhoopJobAdapter:
    source: AutomationSource = "whoop"

    def discover(self, settings: Settings) -> Iterable[AutomationJob]:
        with session_scope(build_engine(settings)) as session:
            rows = session.execute(
                select(
                    WhoopConnection.profile_id, WhoopConnection.account_name
                ).order_by(WhoopConnection.profile_id, WhoopConnection.account_name)
            ).all()
        return tuple(
            AutomationJob(
                "whoop",
                str(profile_id),
                account_name,
                True,
                (
                    "whoop",
                    "sync",
                    "--profile-id",
                    str(profile_id),
                    "--account",
                    account_name,
                ),
            )
            for profile_id, account_name in rows
        )


def _profile_directories(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("unsafe_configuration")
    directories: list[Path] = []
    for entry in root.iterdir():
        entry_info = entry.lstat()
        if stat.S_ISLNK(entry_info.st_mode):
            raise RuntimeError("unsafe_configuration")
        if stat.S_ISDIR(entry_info.st_mode):
            directories.append(entry)
    return tuple(sorted(directories, key=lambda value: value.name))


def _has_profile_file(directory: Path) -> bool:
    path = directory / "profile.json"
    if path.is_symlink():
        raise RuntimeError("unsafe_configuration")
    return path.exists()


def _token_present(path: Path, connector_root: Path) -> bool:
    current = path.parent
    while True:
        if current.is_symlink():
            raise RuntimeError("unsafe_configuration")
        if current.exists() and not current.is_dir():
            raise RuntimeError("unsafe_configuration")
        if current == connector_root:
            break
        if connector_root not in current.parents:
            raise RuntimeError("unsafe_configuration")
        current = current.parent
    if path.is_symlink():
        raise RuntimeError("unsafe_configuration")
    if not path.exists():
        return False
    if not path.is_file():
        raise RuntimeError("unsafe_configuration")
    return True


@dataclass(frozen=True, slots=True)
class GmailJobAdapter:
    source: AutomationSource = "gmail"

    def discover(self, settings: Settings) -> Iterable[AutomationJob]:
        store = LocalGmailProfileStore(settings.gmail_root)
        jobs: list[AutomationJob] = []
        for directory in _profile_directories(settings.gmail_root):
            if not _has_profile_file(directory):
                continue
            profile = store.load(directory.name)
            for account in profile.accounts:
                token_path = (
                    settings.gmail_root
                    / profile.profile_id
                    / "accounts"
                    / account.account_id
                    / "token.json"
                )
                jobs.append(
                    AutomationJob(
                        "gmail",
                        profile.profile_id,
                        account.account_id,
                        True,
                        (
                            "gmail",
                            "sync",
                            profile.profile_id,
                            "--account-id",
                            account.account_id,
                        ),
                        (
                            None
                            if _token_present(token_path, settings.gmail_root)
                            else "oauth_not_ready"
                        ),
                    )
                )
        return tuple(jobs)


@dataclass(frozen=True, slots=True)
class DriveJobAdapter:
    source: AutomationSource = "drive"

    def discover(self, settings: Settings) -> Iterable[AutomationJob]:
        store = LocalProfileStore(settings.google_drive_root)
        return tuple(
            AutomationJob(
                "drive",
                profile.profile_id,
                "main",
                True,
                ("drive", "sync", profile.profile_id),
                (
                    None
                    if _token_present(
                        directory / "token.json", settings.google_drive_root
                    )
                    else "oauth_not_ready"
                ),
            )
            for directory in _profile_directories(settings.google_drive_root)
            if _has_profile_file(directory)
            for profile in (store.load(directory.name),)
        )


@dataclass(frozen=True, slots=True)
class SheetsJobAdapter:
    source: AutomationSource = "sheets"

    def discover(self, settings: Settings) -> Iterable[AutomationJob]:
        store = LocalSheetsProfileStore(settings.google_sheets_root)
        return tuple(
            AutomationJob(
                "sheets",
                profile.profile_id,
                "main",
                False,
                ("sheets", "sync", profile.profile_id),
                (
                    None
                    if _token_present(
                        directory / "token.json", settings.google_sheets_root
                    )
                    else "oauth_not_ready"
                ),
            )
            for directory in _profile_directories(settings.google_sheets_root)
            if _has_profile_file(directory)
            for profile in (store.load(directory.name),)
        )


def configured_job_adapters(
    settings: Settings, executable: Path
) -> tuple[JobAdapter, ...]:
    del settings, executable
    return (WhoopJobAdapter(), GmailJobAdapter(), DriveJobAdapter(), SheetsJobAdapter())
