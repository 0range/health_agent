from __future__ import annotations

import stat

from typer.testing import CliRunner

import health_agent.cli as cli_module
from health_agent.cli import app


def test_configure_token_uses_hidden_prompt_and_private_file(
    tmp_path, monkeypatch
) -> None:
    token = "123456:cli-secret-value"
    monkeypatch.setenv("TELEGRAM_ROOT", str(tmp_path / "telegram"))

    result = CliRunner().invoke(
        app, ["telegram", "configure-token"], input=token + "\n"
    )

    assert result.exit_code == 0
    assert "status=configured" in result.stdout
    assert token not in result.stdout
    token_path = tmp_path / "telegram" / "bot-token"
    assert token_path.read_text() == token
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_status_is_explicitly_local_and_never_prints_token(
    tmp_path, monkeypatch
) -> None:
    token = "123456:cli-secret-value"
    root = tmp_path / "telegram"
    root.mkdir()
    (root / "bot-token").write_text(token)
    monkeypatch.setenv("TELEGRAM_ROOT", str(root))

    result = CliRunner().invoke(app, ["telegram", "status"])

    assert result.exit_code == 0
    assert "token_configured=true" in result.stdout
    assert "identity_bound=false" in result.stdout
    assert token not in result.stdout


def test_discover_id_prints_only_private_numeric_ids(tmp_path, monkeypatch) -> None:
    token = "123456:cli-secret-value"
    root = tmp_path / "telegram"
    root.mkdir()
    (root / "bot-token").write_text(token)
    monkeypatch.setenv("TELEGRAM_ROOT", str(root))

    class FakeAPI:
        def __init__(self, received_token: str) -> None:
            assert received_token == token

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

    monkeypatch.setattr(cli_module, "TelegramBotAPI", FakeAPI)

    result = CliRunner().invoke(app, ["telegram", "discover-id"])

    assert result.exit_code == 0
    assert result.stdout == "telegram_user_id=77 private_chat_id=77\n"
    assert "sensitive" not in result.stdout
    assert "private-name" not in result.stdout
    assert token not in result.stdout
