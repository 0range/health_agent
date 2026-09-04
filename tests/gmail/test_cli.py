from __future__ import annotations

import base64
from pathlib import Path

import pymupdf
import pytest
from google.oauth2.credentials import Credentials
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from health_agent.cli import app
from health_agent.db import session_scope
from health_agent.gmail.config import GMAIL_READONLY_SCOPE
from health_agent.gmail.service import GmailAccountMismatch
from health_agent.gmail.stores import LocalGmailProfileStore, LocalGmailTokenStore
from health_agent.gmail.types import (
    EncodedBody,
    GmailMessage,
    GmailPart,
    MailboxProfile,
    MessagePage,
)
from health_agent.models import DEFAULT_PROFILE_ID, Document, ReviewItem, SourceRecord

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
    assert "account=personal oauth=missing" in status.stdout
    assert "account=work oauth=missing" in status.stdout
    assert "oauth_mode=testing" in status.stdout
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

    status = runner.invoke(app, ["gmail", "status", PROFILE])
    assert status.stdout.count("last_error=oauth_required") == 2
    assert status.stdout.count("oauth=reauth_required") == 2
    assert "last_attempt=never" not in status.stdout


def test_reauthorization_mismatch_preserves_previous_verified_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gmail"
    monkeypatch.setenv("GMAIL_ROOT", str(root))
    runner = CliRunner()
    runner.invoke(app, ["gmail", "configure", PROFILE, "personal"])
    tokens = LocalGmailTokenStore(root)
    old = Credentials(token="old", scopes=[GMAIL_READONLY_SCOPE])
    tokens.publish_verified(PROFILE, "personal", "alice@example.com", old.to_json())
    before = tokens.path_for(PROFILE, "personal").read_bytes()
    replacement = Credentials(token="new", scopes=[GMAIL_READONLY_SCOPE])

    monkeypatch.setattr(
        "health_agent.cli.GmailOAuth.stage",
        lambda self, profile_id, account_id, **kwargs: replacement,
    )

    class WrongMailbox:
        def get_profile(self) -> MailboxProfile:
            return MailboxProfile("bob@example.com", "10")

    monkeypatch.setattr(
        "health_agent.cli.GoogleGmailGateway.from_credentials",
        lambda credentials, **kwargs: WrongMailbox(),
    )

    result = runner.invoke(app, ["gmail", "auth", PROFILE, "personal"])

    assert result.exit_code == 1
    assert isinstance(result.exception, GmailAccountMismatch)
    assert tokens.path_for(PROFILE, "personal").read_bytes() == before


def test_status_reports_symlinked_token_as_invalid_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gmail"
    monkeypatch.setenv("GMAIL_ROOT", str(root))
    runner = CliRunner()
    runner.invoke(app, ["gmail", "configure", PROFILE, "personal"])
    tokens = LocalGmailTokenStore(root)
    target = tmp_path / "outside-token.json"
    target.write_text('{"private": "unchanged"}', encoding="utf-8")
    tokens.path_for(PROFILE, "personal").symlink_to(target)

    result = runner.invoke(app, ["gmail", "status", PROFILE])

    assert result.exit_code == 0, (result.stdout, result.exception)
    assert "oauth=invalid" in result.stdout
    assert target.read_text(encoding="utf-8") == '{"private": "unchanged"}'


def test_production_sync_cli_reaches_common_database_and_review_pipeline(
    clean_database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gmail"
    monkeypatch.setenv("GMAIL_ROOT", str(root))
    monkeypatch.setenv(
        "DATABASE_URL", clean_database.url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEMPORARY_ROOT", str(tmp_path / "tmp"))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    runner.invoke(app, ["gmail", "configure", profile_id, "personal"])
    credentials = Credentials(token="token", scopes=[GMAIL_READONLY_SCOPE])
    LocalGmailTokenStore(root).publish_verified(
        profile_id, "personal", "alice@example.com", credentials.to_json()
    )
    pdf_path = tmp_path / "labs.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Ferritin 42 ng/mL 30-400")
    document.save(pdf_path)
    document.close()
    content = pdf_path.read_bytes()

    class Mailbox:
        def get_profile(self) -> MailboxProfile:
            return MailboxProfile("alice@example.com", "100")

        def list_messages(self, query: str, page_token: str | None) -> MessagePage:
            return MessagePage(("m1",), None)

        def get_message(self, message_id: str) -> GmailMessage:
            return GmailMessage(
                "m1",
                "t1",
                "90",
                1000,
                "Your files",
                "clinic@example.com",
                GmailPart(
                    "",
                    "multipart/mixed",
                    "",
                    None,
                    None,
                    children=(
                        GmailPart(
                            "1", "application/pdf", "document.pdf", "a1", len(content)
                        ),
                    ),
                ),
                ("INBOX",),
            )

        def attachment_data(self, message_id: str, attachment_id: str) -> EncodedBody:
            return EncodedBody(
                base64.urlsafe_b64encode(content).decode().rstrip("="), len(content)
            )

        def list_history(self, history_id: str, page_token: str | None) -> object:
            raise AssertionError("initial scan must not request history")

    monkeypatch.setattr(
        "health_agent.cli.GmailOAuth.stage",
        lambda self, profile_id, account_id, **kwargs: credentials,
    )
    monkeypatch.setattr(
        "health_agent.cli.GoogleGmailGateway.from_credentials",
        lambda credentials, **kwargs: Mailbox(),
    )

    result = runner.invoke(
        app, ["gmail", "sync", profile_id, "--account-id", "personal"]
    )

    assert result.exit_code == 0, (result.stdout, result.exception)
    assert "medically_imported=1" in result.stdout
    assert "staged=1" in result.stdout
    with session_scope(clean_database) as database:
        assert len(database.scalars(select(Document)).all()) == 1
        assert len(database.scalars(select(ReviewItem)).all()) == 1
        source = database.scalars(select(SourceRecord)).one()
        assert source.provider == "gmail"


@pytest.mark.parametrize(
    ("body_text", "reason", "provider"),
    (
        (
            "Приём у терапевта завтра",
            "appointment",
            "gmail_body_appointment",
        ),
        (
            "Ваши лабораторные анализы готовы",
            "body_medical",
            "gmail_body_medical",
        ),
    ),
)
def test_body_only_item_enters_common_inbox_and_safe_attention_cli(
    clean_database: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_text: str,
    reason: str,
    provider: str,
) -> None:
    root = tmp_path / "gmail"
    profile_id = str(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("GMAIL_ROOT", str(root))
    monkeypatch.setenv(
        "DATABASE_URL", clean_database.url.render_as_string(hide_password=False)
    )
    runner = CliRunner()
    runner.invoke(app, ["gmail", "configure", profile_id, "personal"])
    credentials = Credentials(token="token", scopes=[GMAIL_READONLY_SCOPE])
    LocalGmailTokenStore(root).publish_verified(
        profile_id, "personal", "alice@example.com", credentials.to_json()
    )
    body = body_text.encode()

    class Mailbox:
        def get_profile(self) -> MailboxProfile:
            return MailboxProfile("alice@example.com", "100")

        def list_messages(self, query: str, page_token: str | None) -> MessagePage:
            return MessagePage(("m-body",), None)

        def get_message(self, message_id: str) -> GmailMessage:
            return GmailMessage(
                "m-body",
                "t1",
                "90",
                1000,
                "Reminder",
                "clinic@example.com",
                GmailPart(
                    "",
                    "text/plain",
                    "",
                    None,
                    len(body),
                    base64.urlsafe_b64encode(body).decode().rstrip("="),
                ),
                ("INBOX",),
            )

        def list_history(self, history_id: str, page_token: str | None) -> object:
            raise AssertionError("initial scan must not request history")

    monkeypatch.setattr(
        "health_agent.cli.GmailOAuth.stage",
        lambda self, profile_id, account_id, **kwargs: credentials,
    )
    monkeypatch.setattr(
        "health_agent.cli.GoogleGmailGateway.from_credentials",
        lambda credentials, **kwargs: Mailbox(),
    )

    sync = runner.invoke(app, ["gmail", "sync", profile_id, "--account-id", "personal"])
    attention = runner.invoke(app, ["gmail", "attention", profile_id])

    assert sync.exit_code == 0, (sync.stdout, sync.exception)
    assert "attention=1" in sync.stdout
    assert f"kind=message reason={reason}" in attention.stdout
    assert body_text not in attention.stdout
    assert "clinic@example.com" not in attention.stdout
    with session_scope(clean_database) as database:
        source = database.scalars(select(SourceRecord)).one()
        assert source.provider == provider


def test_image_ocr_reason_is_truthful_in_sync_status_and_attention_cli(
    clean_database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gmail"
    profile_id = str(DEFAULT_PROFILE_ID)
    monkeypatch.setenv("GMAIL_ROOT", str(root))
    monkeypatch.setenv(
        "DATABASE_URL", clean_database.url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEMPORARY_ROOT", str(tmp_path / "tmp"))
    runner = CliRunner()
    runner.invoke(app, ["gmail", "configure", profile_id, "personal"])
    credentials = Credentials(token="token", scopes=[GMAIL_READONLY_SCOPE])
    LocalGmailTokenStore(root).publish_verified(
        profile_id, "personal", "alice@example.com", credentials.to_json()
    )
    content = b"\x89PNG\r\n\x1a\nsynthetic"

    class Mailbox:
        def get_profile(self) -> MailboxProfile:
            return MailboxProfile("alice@example.com", "100")

        def list_messages(self, query: str, page_token: str | None) -> MessagePage:
            return MessagePage(("m-image",), None)

        def get_message(self, message_id: str) -> GmailMessage:
            return GmailMessage(
                "m-image",
                "t1",
                "90",
                1000,
                "Laboratory scan",
                "clinic@example.com",
                GmailPart(
                    "",
                    "multipart/mixed",
                    "",
                    None,
                    None,
                    children=(
                        GmailPart("1", "image/png", "scan.png", "a1", len(content)),
                    ),
                ),
                ("INBOX",),
            )

        def attachment_data(self, message_id: str, attachment_id: str) -> EncodedBody:
            return EncodedBody(
                base64.urlsafe_b64encode(content).decode().rstrip("="), len(content)
            )

        def list_history(self, history_id: str, page_token: str | None) -> object:
            raise AssertionError("initial scan must not request history")

    monkeypatch.setattr(
        "health_agent.cli.GmailOAuth.stage",
        lambda self, profile_id, account_id, **kwargs: credentials,
    )
    monkeypatch.setattr(
        "health_agent.cli.GoogleGmailGateway.from_credentials",
        lambda credentials, **kwargs: Mailbox(),
    )

    sync = runner.invoke(app, ["gmail", "sync", profile_id, "--account-id", "personal"])
    status = runner.invoke(app, ["gmail", "status", profile_id])
    attention = runner.invoke(app, ["gmail", "attention", profile_id])

    assert sync.exit_code == 0, (sync.stdout, sync.exception)
    assert "ocr_required=1" in sync.stdout
    assert "attention=1" in sync.stdout
    assert "medically_imported=0" in sync.stdout
    assert "ocr_required=1" in status.stdout
    assert "reason=image_ocr_required" in attention.stdout
