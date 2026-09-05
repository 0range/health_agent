from __future__ import annotations

from typer.testing import CliRunner

from health_agent import cli
from health_agent.google_sheets.service import SheetsStatus, SheetsSyncReport
from health_agent.models import DEFAULT_PROFILE_ID


class FakeService:
    def sync(self, profile_id):
        assert profile_id == DEFAULT_PROFILE_ID
        return SheetsSyncReport("succeeded", 1, 0, 2, 3, 4, "spreadsheet_123")

    def status(self, profile_id):
        assert profile_id == DEFAULT_PROFILE_ID
        return SheetsStatus(
            True, "ready", True, "succeeded", "2026-09-05T10:00:00+00:00", None
        )


def test_sheets_commands_are_registered() -> None:
    result = CliRunner().invoke(cli.app, ["sheets", "--help"])
    assert result.exit_code == 0
    for command in ("configure", "authorize", "sync", "status"):
        assert command in result.stdout


def test_sync_and_status_print_only_safe_fields(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_build_sheets_service", lambda settings: FakeService())
    sync = CliRunner().invoke(cli.app, ["sheets", "sync", str(DEFAULT_PROFILE_ID)])
    assert sync.exit_code == 0
    assert sync.stdout == (
        f"status=succeeded profile={DEFAULT_PROFILE_ID} spreadsheet=spreadsheet_123 "
        "decisions=1 replayed=0 labs=2 review=3 sources=4\n"
    )
    status = CliRunner().invoke(cli.app, ["sheets", "status", str(DEFAULT_PROFILE_ID)])
    assert status.exit_code == 0
    assert "oauth=ready" in status.stdout
    assert "last_error=none" in status.stdout


def test_sync_failure_redacts_exception(monkeypatch) -> None:
    class Exploding:
        def sync(self, profile_id):
            del profile_id
            raise RuntimeError("private laboratory value and Google payload")

    monkeypatch.setattr(cli, "_build_sheets_service", lambda settings: Exploding())
    result = CliRunner().invoke(cli.app, ["sheets", "sync", str(DEFAULT_PROFILE_ID)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "status=failed safe_error=sheets_sync_failed\n"
    assert "private laboratory" not in result.output
