from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from health_agent.automation.registry import (
    DashboardJobAdapter,
    DriveJobAdapter,
    GmailJobAdapter,
    LabExtractionJobAdapter,
    SheetsJobAdapter,
    WhoopJobAdapter,
)
from health_agent.config import Settings
from health_agent.dashboard_destinations import DashboardDestinationStore
from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.stores import LocalGmailProfileStore, LocalGmailTokenStore
from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.stores import LocalProfileStore, LocalTokenStore
from health_agent.google_drive.types import DriveAccountIdentity
from health_agent.google_sheets.config import SheetsProfile
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsTokenStore,
)
from health_agent.google_sheets.types import SheetsAccountIdentity
from health_agent.models import Profile
from health_agent.whoop.models import WhoopConnection

PROFILE_A = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_B = UUID("00000000-0000-0000-0000-000000000002")


def test_discovers_whoop_connections_with_exact_arguments(
    session: Session, disposable_postgres
) -> None:
    session.add(
        WhoopConnection(
            profile_id=PROFILE_A, account_name="main", auth_status="connected"
        )
    )
    session.commit()
    jobs = tuple(WhoopJobAdapter().discover(disposable_postgres.settings))
    assert [(job.key, job.arguments) for job in jobs] == [
        (
            ("whoop", str(PROFILE_A), "main"),
            ("whoop", "sync", "--profile-id", str(PROFILE_A), "--account", "main"),
        )
    ]


def test_discovers_gmail_accounts_and_drive_profiles_in_stable_order(
    tmp_path: Path,
) -> None:
    gmail_root = tmp_path / "gmail"
    drive_root = tmp_path / "drive"
    gmail = LocalGmailProfileStore(gmail_root)
    gmail.save(
        GmailProfile.empty(PROFILE_B)
        .upsert_account(GmailAccount.create("secondary"))
        .upsert_account(GmailAccount.create("main"))
    )
    gmail.save(
        GmailProfile.empty(PROFILE_A).upsert_account(GmailAccount.create("main"))
    )
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
    assert all(job.not_ready_code == "oauth_not_ready" for job in gmail_jobs)
    assert [job.key for job in drive_jobs] == [
        ("drive", str(PROFILE_A), "main"),
        ("drive", str(PROFILE_B), "main"),
    ]
    assert drive_jobs[0].arguments == ("drive", "sync", str(PROFILE_A))


def test_symlinked_or_malformed_profile_configuration_fails_closed(
    tmp_path: Path,
) -> None:
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


def test_unconfigured_gmail_residue_is_an_empty_source(tmp_path: Path) -> None:
    root = tmp_path / "gmail"
    residue = root / str(PROFILE_A) / "accounts" / "main"
    residue.mkdir(parents=True)
    (residue / "sync.lock").write_text("", encoding="utf-8")

    assert tuple(GmailJobAdapter().discover(Settings(gmail_root=root))) == ()


def test_drive_without_oauth_is_discovered_as_not_ready(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    LocalProfileStore(root).save(
        DriveProfile.create(str(PROFILE_A), ["folder_abcdefghij"])
    )

    job = next(iter(DriveJobAdapter().discover(Settings(google_drive_root=root))))
    assert job.key == ("drive", str(PROFILE_A), "main")
    assert job.not_ready_code == "oauth_not_ready"

    LocalTokenStore(root).publish_verified(
        str(PROFILE_A),
        DriveAccountIdentity("permission-1", "owner@example.com"),
        '{"token":"synthetic"}',
    )
    ready = next(iter(DriveJobAdapter().discover(Settings(google_drive_root=root))))
    assert ready.not_ready_code is None


def test_gmail_token_readiness_is_profile_and_account_scoped(tmp_path: Path) -> None:
    root = tmp_path / "gmail"
    profile = (
        GmailProfile.empty(PROFILE_A)
        .upsert_account(GmailAccount.create("main"))
        .upsert_account(GmailAccount.create("secondary"))
    )
    LocalGmailProfileStore(root).save(profile)
    LocalGmailTokenStore(root).publish_verified(
        str(PROFILE_A),
        "secondary",
        "owner@example.com",
        '{"token":"synthetic"}',
    )

    jobs = {
        job.account_id: job
        for job in GmailJobAdapter().discover(Settings(gmail_root=root))
    }
    assert jobs["main"].not_ready_code == "oauth_not_ready"
    assert jobs["secondary"].not_ready_code is None


def test_gmail_symlinked_token_fails_closed_but_malformed_token_reaches_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gmail"
    LocalGmailProfileStore(root).save(
        GmailProfile.empty(PROFILE_A).upsert_account(GmailAccount.create("main"))
    )
    token = root / str(PROFILE_A) / "accounts" / "main" / "token.json"
    token.parent.mkdir(parents=True)
    outside = tmp_path / "outside-token"
    outside.write_text("{}", encoding="utf-8")
    token.symlink_to(outside)
    with pytest.raises(RuntimeError, match="unsafe_configuration"):
        tuple(GmailJobAdapter().discover(Settings(gmail_root=root)))

    token.unlink()
    token.write_text("not-json", encoding="utf-8")
    token.chmod(0o600)
    job = next(iter(GmailJobAdapter().discover(Settings(gmail_root=root))))
    assert job.not_ready_code is None


def test_sheets_profile_is_discovered_and_missing_oauth_is_deferred(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sheets"
    LocalSheetsProfileStore(root).save(SheetsProfile.create(str(PROFILE_A)))
    settings = Settings(google_sheets_root=root)
    job = next(iter(SheetsJobAdapter().discover(settings)))
    assert job.key == ("sheets", str(PROFILE_A), "main")
    assert job.arguments == ("sheets", "sync", str(PROFILE_A))
    assert job.supports_full is False
    assert job.not_ready_code == "oauth_not_ready"

    LocalSheetsTokenStore(root).publish_verified(
        str(PROFILE_A),
        SheetsAccountIdentity("permission-1", "owner@example.com"),
        '{"token":"synthetic"}',
    )
    ready = next(iter(SheetsJobAdapter().discover(settings)))
    assert ready.not_ready_code is None
def test_discovers_only_enabled_extraction_profiles(clean_database, disposable_postgres):
    from health_agent.db import session_scope
    from health_agent.lab_extraction.models import LabExtractionProfile
    with session_scope(clean_database) as session:
        session.add(LabExtractionProfile(profile_id=PROFILE_A))
    jobs = tuple(LabExtractionJobAdapter().discover(disposable_postgres.settings))
    assert len(jobs) == 1 and not jobs[0].supports_full
    assert jobs[0].arguments == ("lab-extract", "run", str(PROFILE_A))
    with session_scope(clean_database) as session:
        session.get_one(LabExtractionProfile, PROFILE_A).enabled = False
    assert tuple(LabExtractionJobAdapter().discover(disposable_postgres.settings)) == ()


def test_dashboard_discovery_is_db_profile_origin_and_labs_scoped(
    tmp_path: Path, session: Session, disposable_postgres
) -> None:
    session.add(Profile(id=PROFILE_B, name="Configured dashboard profile"))
    session.commit()
    settings = disposable_postgres.settings.model_copy(
        update={
            "connector_state_root": tmp_path / "connectors",
            "metabase_url": "http://127.0.0.1:53000",
        }
    )
    destinations = DashboardDestinationStore(
        settings.connector_state_root, settings.metabase_url
    )
    destinations.save(PROFILE_A, "labs", 7)
    DashboardDestinationStore(
        settings.connector_state_root, "http://127.0.0.1:53001"
    ).save(PROFILE_B, "labs", 8)
    destinations.save(UUID(int=3), "labs", 9)

    jobs = tuple(DashboardJobAdapter().discover(settings))

    assert [(job.key, job.arguments, job.supports_full) for job in jobs] == [
        (
            ("dashboard", str(PROFILE_A), "main"),
            ("dashboard", "setup-labs", "--profile-id", str(PROFILE_A)),
            False,
        )
    ]


def test_unconfigured_dashboard_profiles_produce_no_job(
    tmp_path: Path, disposable_postgres
) -> None:
    settings = disposable_postgres.settings.model_copy(
        update={"connector_state_root": tmp_path / "connectors"}
    )

    assert tuple(DashboardJobAdapter().discover(settings)) == ()
