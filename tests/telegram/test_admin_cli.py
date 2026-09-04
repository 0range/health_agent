from __future__ import annotations

import stat
from datetime import UTC, datetime

from typer.testing import CliRunner

import health_agent.cli as cli_module
from health_agent.cli import app
from health_agent.config import Settings
from health_agent.telegram.admin import TelegramAdminService
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import VerifiedBotCredential

TOKEN = "123456:cli-secret-value"
BOT_ID = 111


class EmptyProfiles:
    def exists(self, profile_id) -> bool:
        return False


class FakeAPI:
    def __init__(self, token: str) -> None:
        assert token == TOKEN

    def get_me(self) -> dict[str, object]:
        return {"id": BOT_ID, "is_bot": True, "username": "health_test_bot"}

    def get_webhook_url(self) -> str:
        return ""

    def get_updates(self, *, offset, timeout_seconds):
        assert offset is None
        assert timeout_seconds == 1
        return (
            {
                "update_id": 1,
                "message": {
                    "text": "sensitive question",
                    "from": {"id": 77, "username": "private-name"},
                    "chat": {"id": 77, "type": "private"},
                },
            },
            {
                "update_id": 2,
                "message": {
                    "from": {"id": 88},
                    "chat": {"id": -100, "type": "group"},
                },
            },
        )


def _admin(tmp_path) -> TelegramAdminService:
    return TelegramAdminService(
        PrivateBotTokenStore(tmp_path / "telegram" / "bot-token"),
        SqliteTelegramState(tmp_path / "telegram" / "state.sqlite3"),
        EmptyProfiles(),
        gateway_factory=FakeAPI,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )


def test_configure_token_uses_hidden_prompt_verifies_and_writes_private_file(
    tmp_path, monkeypatch
) -> None:
    service = _admin(tmp_path)
    monkeypatch.setenv("TELEGRAM_ROOT", str(tmp_path / "telegram"))
    monkeypatch.setattr(cli_module, "_telegram_admin", lambda _: service)

    result = CliRunner().invoke(
        app, ["telegram", "configure-token"], input=TOKEN + "\n"
    )

    assert result.exit_code == 0
    assert "status=verified" in result.stdout
    assert "bot_id=111" in result.stdout
    assert TOKEN not in result.stdout
    token_path = tmp_path / "telegram" / "bot-token"
    assert service.tokens.load_verified().bot_id == BOT_ID
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_status_reports_remote_verification_not_just_file_presence(
    tmp_path, monkeypatch
) -> None:
    service = _admin(tmp_path)
    service.configure_token(TOKEN)
    monkeypatch.setenv("TELEGRAM_ROOT", str(tmp_path / "telegram"))
    monkeypatch.setattr(cli_module, "_telegram_admin", lambda _: service)

    result = CliRunner().invoke(app, ["telegram", "status"])

    assert result.exit_code == 0
    assert "token_configured=true" in result.stdout
    assert "credential_verified=true" in result.stdout
    assert "poller_running=false" in result.stdout
    assert TOKEN not in result.stdout


def test_discover_id_prints_only_private_numeric_ids(tmp_path, monkeypatch) -> None:
    root = tmp_path / "telegram"
    root.mkdir()
    PrivateBotTokenStore(root / "bot-token").save_verified(
        VerifiedBotCredential(TOKEN, BOT_ID, "health_test_bot")
    )
    state = SqliteTelegramState(root / "state.sqlite3")
    state.register_bot(BOT_ID, "health_test_bot")
    monkeypatch.setenv("TELEGRAM_ROOT", str(root))
    monkeypatch.setattr(cli_module, "TelegramBotAPI", FakeAPI)

    result = CliRunner().invoke(app, ["telegram", "discover-id"])

    assert result.exit_code == 0
    assert result.stdout == "telegram_user_id=77 private_chat_id=77\n"
    assert "sensitive" not in result.stdout
    assert "private-name" not in result.stdout
    assert TOKEN not in result.stdout


def test_staging_token_and_state_endpoints_are_independently_overridable(
    tmp_path,
) -> None:
    settings = Settings(
        telegram_root=tmp_path / "unused",
        telegram_token_file=tmp_path / "test-token",
        telegram_state_path=tmp_path / "test-state.sqlite3",
        telegram_staging_path=tmp_path / "test-staging",
    )

    assert settings.effective_telegram_token_file == tmp_path / "test-token"
    assert settings.telegram_state_file == tmp_path / "test-state.sqlite3"
    assert settings.telegram_staging_root == tmp_path / "test-staging"
