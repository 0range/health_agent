from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pymupdf
import pytest
from google.oauth2.credentials import Credentials
from sqlalchemy import Engine, select, text
from typer.testing import CliRunner

from health_agent.cli import app, configure_drive
from health_agent.db import session_scope
from health_agent.google_drive.config import DRIVE_READONLY_SCOPE
from health_agent.google_drive.service import FOLDER_MIME_TYPE
from health_agent.google_drive.stores import (
    LocalProfileStore,
    LocalSyncStateStore,
    LocalTokenStore,
)
from health_agent.google_drive.types import (
    ChangePage,
    DriveAccountIdentity,
    DriveItem,
    ItemPage,
)
from health_agent.importer import approve_observation
from health_agent.metabase import LAB_HISTORY_QUERY
from health_agent.models import DEFAULT_PROFILE_ID, Document, LabObservation


class CliDriveGateway:
    def __init__(self, pdf: bytes = b"") -> None:
        self.root = DriveItem(
            "root-folder-123",
            "Health",
            FOLDER_MIME_TYPE,
            (),
            can_download=False,
        )
        self.lab = DriveItem(
            "lab-file-123",
            "labs.pdf",
            "application/pdf",
            (self.root.file_id,),
            version="1",
            size_bytes=len(pdf),
            can_download=True,
            web_view_link="https://drive.google.com/file/d/lab-file-123/view",
        )
        self.pdf = pdf

    def account_identity(self) -> DriveAccountIdentity:
        return DriveAccountIdentity("permission-a", "alice@example.com")

    def get_file(self, file_id: str) -> DriveItem:
        return {self.root.file_id: self.root, self.lab.file_id: self.lab}[file_id]

    def list_children(self, folder_id: str, page_token: str | None) -> ItemPage:
        assert folder_id == self.root.file_id
        return ItemPage((self.lab,), None)

    def get_start_page_token(self) -> str:
        return "cursor-1"

    def list_changes(self, page_token: str) -> ChangePage:
        return ChangePage((), None, "cursor-2")

    def download_chunks(
        self, item: DriveItem, export_media_type: str | None
    ):
        yield self.pdf[:50]
        yield self.pdf[50:]


def _lab_pdf(tmp_path: Path) -> bytes:
    path = tmp_path / "dated-lab.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text(
            (72, 72),
            "Collection date: 2024-05-06\nFerritin 42 ng/mL 30-400",
        )
        document.save(path)
    return path.read_bytes()


def _configure_with_token(
    runner: CliRunner,
    drive_root: Path,
    credentials: Credentials,
) -> None:
    profile_id = str(DEFAULT_PROFILE_ID)
    result = runner.invoke(
        app,
        ["drive", "configure", profile_id, "root-folder-123"],
    )
    assert result.exit_code == 0
    LocalTokenStore(drive_root).publish_verified(
        profile_id,
        DriveAccountIdentity("permission-a", "alice@example.com"),
        credentials.to_json(),
    )


def test_configure_and_status_are_profile_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(tmp_path / "drive"))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    root = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"

    profile_id = str(DEFAULT_PROFILE_ID)
    configured = runner.invoke(app, ["drive", "configure", profile_id, root])
    status = runner.invoke(app, ["drive", "status", profile_id])

    assert configured.exit_code == 0
    assert f"status=configured profile={profile_id} roots=1" in configured.stdout
    assert status.exit_code == 0
    assert f"profile={profile_id}" in status.stdout
    assert "token=missing" in status.stdout
    assert "account_bound=no" in status.stdout
    assert "cursor=none" in status.stdout


def test_changing_roots_invalidates_old_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    profile_id = str(DEFAULT_PROFILE_ID)
    first = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    second = "2g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsC"
    assert runner.invoke(app, ["drive", "configure", profile_id, first]).exit_code == 0
    state = LocalSyncStateStore(drive_root)
    state.set_cursor(profile_id, "old-root-cursor")

    result = runner.invoke(app, ["drive", "configure", profile_id, second])

    assert result.exit_code == 0
    assert state.get_cursor(profile_id) is None


def test_reconfiguring_roots_preserves_verified_account_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv(
        "DATABASE_URL", clean_database.url.render_as_string(hide_password=False)
    )
    runner = CliRunner()
    profile_id = str(DEFAULT_PROFILE_ID)
    store = LocalProfileStore(drive_root)
    first = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    second = "2g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsC"
    assert runner.invoke(app, ["drive", "configure", profile_id, first]).exit_code == 0
    store.save(
        store.load(profile_id).with_account("permission-a", "alice@example.com")
    )

    result = runner.invoke(app, ["drive", "configure", profile_id, second])

    assert result.exit_code == 0
    configured = store.load(profile_id)
    assert configured.account_permission_id == "permission-a"
    assert configured.account_email == "alice@example.com"


def test_root_reconfiguration_waits_for_the_same_lock_as_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    first = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    second = "2g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsC"
    configure_drive(DEFAULT_PROFILE_ID, [first])
    state = LocalSyncStateStore(drive_root)
    state.set_cursor(profile_id, "old-root-cursor")
    started = Event()
    finished = Event()
    failures: list[Exception] = []

    def reconfigure() -> None:
        started.set()
        try:
            configure_drive(DEFAULT_PROFILE_ID, [second])
        except Exception as error:  # noqa: BLE001 - surfaced to the test thread
            failures.append(error)
        finally:
            finished.set()

    with state.sync_lock(profile_id):
        thread = Thread(target=reconfigure)
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
        assert LocalProfileStore(drive_root).load(profile_id).root_folder_ids == (first,)
        assert state.get_cursor(profile_id) == "old-root-cursor"
    thread.join(timeout=2)

    assert not failures
    assert finished.is_set()
    assert LocalProfileStore(drive_root).load(profile_id).root_folder_ids == (second,)
    assert state.get_cursor(profile_id) is None


def test_configure_unknown_database_profile_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(tmp_path / "drive"))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    unknown = "22222222-2222-4222-8222-222222222222"
    result = CliRunner().invoke(
        app,
        ["drive", "configure", unknown, "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"],
    )

    assert result.exit_code != 0
    assert "profile does not exist" in result.output


def test_status_reports_invalid_token_without_remote_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    assert runner.invoke(app, ["drive", "configure", profile_id, folder]).exit_code == 0
    token_path = LocalTokenStore(drive_root).path_for(profile_id)
    token_path.write_text("not-json", encoding="utf-8")

    result = runner.invoke(app, ["drive", "status", profile_id])

    assert result.exit_code == 0
    assert "token=invalid" in result.stdout
    assert "account_bound=no" in result.stdout


def test_status_exposes_interrupted_sync_without_remote_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    assert runner.invoke(app, ["drive", "configure", profile_id, folder]).exit_code == 0
    LocalSyncStateStore(drive_root).begin_sync(profile_id, "full")

    result = runner.invoke(app, ["drive", "status", profile_id])

    assert result.exit_code == 0
    assert "sync_state=interrupted" in result.stdout
    assert "last_attempt=" in result.stdout


@pytest.mark.parametrize("failure", ["lookup", "mismatch"])
def test_failed_reauthorization_preserves_previous_verified_token(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_database: Engine,
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    assert runner.invoke(app, ["drive", "configure", profile_id, folder]).exit_code == 0
    tokens = LocalTokenStore(drive_root)
    old_credentials = Credentials(token="old", scopes=[DRIVE_READONLY_SCOPE])
    token_path = tokens.publish_verified(
        profile_id,
        DriveAccountIdentity("permission-old", "old@example.com"),
        old_credentials.to_json(),
    )
    original = token_path.read_bytes()
    staged = Credentials(token="new", scopes=[DRIVE_READONLY_SCOPE])
    monkeypatch.setattr("health_agent.cli.DriveOAuth.stage", lambda *args, **kwargs: staged)

    if failure == "lookup":
        def fail_lookup(credentials: Credentials, **kwargs: object) -> object:
            raise RuntimeError("identity lookup failed")

        monkeypatch.setattr(
            "health_agent.cli.GoogleDriveGateway.from_credentials", fail_lookup
        )
    else:
        class Gateway:
            def account_identity(self) -> DriveAccountIdentity:
                return DriveAccountIdentity("permission-new", "new@example.com")

        monkeypatch.setattr(
            "health_agent.cli.GoogleDriveGateway.from_credentials",
            lambda credentials, **kwargs: Gateway(),
        )

    result = runner.invoke(app, ["drive", "auth", profile_id])

    assert result.exit_code != 0
    assert token_path.read_bytes() == original


def test_successful_auth_publishes_verified_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_database: Engine,
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    profile_id = str(DEFAULT_PROFILE_ID)
    assert runner.invoke(
        app, ["drive", "configure", profile_id, "root-folder-123"]
    ).exit_code == 0
    credentials = Credentials(token="new", scopes=[DRIVE_READONLY_SCOPE])
    gateway = CliDriveGateway()
    monkeypatch.setattr(
        "health_agent.cli.DriveOAuth.stage", lambda *args, **kwargs: credentials
    )
    monkeypatch.setattr(
        "health_agent.cli.GoogleDriveGateway.from_credentials",
        lambda credentials, **kwargs: gateway,
    )

    result = runner.invoke(app, ["drive", "auth", profile_id])

    assert result.exit_code == 0
    assert "status=authorized" in result.stdout
    verified = LocalTokenStore(drive_root).load_verified(profile_id)
    assert verified is not None
    assert verified[0].permission_id == "permission-a"
    profile = LocalProfileStore(drive_root).load(profile_id)
    assert profile.account_permission_id == "permission-a"
    assert profile.account_email == "alice@example.com"


def test_cli_sync_full_drive_lab_approve_reaches_exact_metabase_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_database: Engine,
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEMPORARY_ROOT", str(tmp_path / "tmp"))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    credentials = Credentials(token="ready", scopes=[DRIVE_READONLY_SCOPE])
    _configure_with_token(runner, drive_root, credentials)
    gateway = CliDriveGateway(_lab_pdf(tmp_path))
    monkeypatch.setattr(
        "health_agent.cli.DriveOAuth.stage", lambda *args, **kwargs: credentials
    )
    monkeypatch.setattr(
        "health_agent.cli.GoogleDriveGateway.from_credentials",
        lambda credentials, **kwargs: gateway,
    )
    profile_id = str(DEFAULT_PROFILE_ID)

    first = runner.invoke(app, ["drive", "sync", profile_id])

    assert first.exit_code == 0
    assert "mode=full" in first.stdout
    assert "medically_imported=1" in first.stdout
    profile = LocalProfileStore(drive_root).load(profile_id)
    assert profile.account_permission_id == "permission-a"
    assert profile.account_email == "alice@example.com"
    with session_scope(clean_database) as session:
        observation_id, document_id = session.execute(
            select(LabObservation.id, Document.id).join(LabObservation.document)
        ).one()
        assert observation_id is not None
        document = session.get_one(Document, document_id)
        assert document.collected_date is not None
        assert document.collected_date.isoformat() == "2024-05-06"

    corrected_date = runner.invoke(
        app,
        [
            "review",
            "set-date",
            str(document_id),
            "--collected-date",
            "2024-05-06",
        ],
    )
    assert corrected_date.exit_code == 0
    assert "status=date_set" in corrected_date.stdout

    with session_scope(clean_database) as session:
        approve_observation(session, observation_id)
    with clean_database.connect() as connection:
        row = connection.execute(text(LAB_HISTORY_QUERY)).one()
    assert row.date.isoformat() == "2024-05-06"
    assert row.canonical_name == "ferritin"
    assert str(row.normalized_value) == "42"

    full = runner.invoke(app, ["drive", "sync", profile_id, "--full"])

    assert full.exit_code == 0
    assert "mode=full" in full.stdout
    assert "unchanged=1" in full.stdout


def test_cli_sync_failure_is_nonzero_and_records_safe_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_database: Engine,
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    credentials = Credentials(token="ready", scopes=[DRIVE_READONLY_SCOPE])
    _configure_with_token(runner, drive_root, credentials)
    gateway = CliDriveGateway()
    gateway.account_identity = lambda: DriveAccountIdentity(  # type: ignore[method-assign]
        "permission-other", "other@example.com"
    )
    monkeypatch.setattr(
        "health_agent.cli.DriveOAuth.stage", lambda *args, **kwargs: credentials
    )
    monkeypatch.setattr(
        "health_agent.cli.GoogleDriveGateway.from_credentials",
        lambda credentials, **kwargs: gateway,
    )
    profile_id = str(DEFAULT_PROFILE_ID)

    result = runner.invoke(app, ["drive", "sync", profile_id])

    assert result.exit_code != 0
    run = LocalSyncStateStore(drive_root).run_state(profile_id)
    assert run["last_error_code"] == "processing_failed"
