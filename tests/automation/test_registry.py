from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from health_agent.automation.registry import (
    DriveJobAdapter,
    GmailJobAdapter,
    WhoopJobAdapter,
)
from health_agent.config import Settings
from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.stores import LocalGmailProfileStore
from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.stores import LocalProfileStore
from health_agent.whoop.models import WhoopConnection

PROFILE_A = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_B = UUID("00000000-0000-0000-0000-000000000002")


def test_discovers_whoop_connections_with_exact_arguments(
    session: Session, disposable_postgres
) -> None:
    session.add(
        WhoopConnection(profile_id=PROFILE_A, account_name="main", auth_status="connected")
    )
    session.commit()
    jobs = tuple(WhoopJobAdapter().discover(disposable_postgres.settings))
    assert [(job.key, job.arguments) for job in jobs] == [
        (
            ("whoop", str(PROFILE_A), "main"),
            ("whoop", "sync", "--profile-id", str(PROFILE_A), "--account", "main"),
        )
    ]


def test_discovers_gmail_accounts_and_drive_profiles_in_stable_order(tmp_path: Path) -> None:
    gmail_root = tmp_path / "gmail"
    drive_root = tmp_path / "drive"
    gmail = LocalGmailProfileStore(gmail_root)
    gmail.save(
        GmailProfile.empty(PROFILE_B)
        .upsert_account(GmailAccount.create("secondary"))
        .upsert_account(GmailAccount.create("main"))
    )
    gmail.save(GmailProfile.empty(PROFILE_A).upsert_account(GmailAccount.create("main")))
    drive = LocalProfileStore(drive_root)
    drive.save(DriveProfile.create(str(PROFILE_B), ["folder_1234567890"]))
    drive.save(DriveProfile.create(str(PROFILE_A), ["folder_abcdefghij"]))
    settings = Settings(gmail_root=gmail_root, google_drive_root=drive_root)

    gmail_jobs = sorted(GmailJobAdapter().discover(settings), key=lambda job: job.key)
    drive_jobs = tuple(DriveJobAdapter().discover(settings))

    assert [job.key for job in gmail_jobs] == [
        ("gmail", str(PROFILE_A), "main"),
        ("gmail", str(PROFILE_B), "main"),
        ("gmail", str(PROFILE_B), "secondary"),
    ]
    assert gmail_jobs[0].arguments == (
        "gmail",
        "sync",
        str(PROFILE_A),
        "--account-id",
        "main",
    )
    assert [job.key for job in drive_jobs] == [
        ("drive", str(PROFILE_A), "main"),
        ("drive", str(PROFILE_B), "main"),
    ]
    assert drive_jobs[0].arguments == ("drive", "sync", str(PROFILE_A))


def test_symlinked_or_malformed_profile_configuration_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    root = tmp_path / "gmail"
    root.mkdir()
    (root / str(PROFILE_A)).symlink_to(target, target_is_directory=True)
    settings = Settings(gmail_root=root)
    with pytest.raises(RuntimeError, match="unsafe_configuration"):
        tuple(GmailJobAdapter().discover(settings))

    bad_root = tmp_path / "drive"
    LocalProfileStore(bad_root).save(
        DriveProfile.create(str(PROFILE_A), ["folder_abcdefghij"])
    )
    (bad_root / str(PROFILE_A) / "profile.json").write_text("{}", encoding="utf-8")
    with pytest.raises((KeyError, TypeError, ValueError)):
        tuple(DriveJobAdapter().discover(Settings(google_drive_root=bad_root)))
