from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from health_agent.cli import app
from health_agent.gmail.stores import LocalGmailProfileStore

PROFILE = "11111111-1111-1111-1111-111111111111"


def test_configure_multiple_accounts_and_show_safe_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GMAIL_ROOT", str(tmp_path / "gmail"))
    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "gmail",
            "configure",
            PROFILE,
            "personal",
            "--trusted-sender",
            "lab@example.com",
        ],
    )
    second = runner.invoke(
        app, ["gmail", "configure", PROFILE, "work", "--lookback-days", "14"]
    )
    status = runner.invoke(app, ["gmail", "status", PROFILE])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert status.exit_code == 0
    assert "account=personal authorized=no" in status.stdout
    assert "account=work authorized=no" in status.stdout
    assert "cursor=none" in status.stdout
    assert "lab@example.com" not in status.stdout
    assert "token" not in status.stdout.casefold()

    runner.invoke(app, ["gmail", "configure", PROFILE, "personal"])
    stored = LocalGmailProfileStore(tmp_path / "gmail").load(PROFILE)
    assert stored.account("personal").trusted_senders == ("lab@example.com",)


def test_sync_all_accounts_reports_oauth_needed_without_trace_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GMAIL_ROOT", str(tmp_path / "gmail"))
    runner = CliRunner()
    runner.invoke(app, ["gmail", "configure", PROFILE, "personal"])
    runner.invoke(app, ["gmail", "configure", PROFILE, "work"])

    result = runner.invoke(app, ["gmail", "sync", PROFILE])

    assert result.exit_code == 1
    assert result.stdout.count("safe_error=oauth_required") == 2
    assert "Traceback" not in result.stdout
    assert "token" not in result.stdout.casefold()
