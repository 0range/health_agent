from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from health_agent import cli
from health_agent.config import Settings


def test_panel_settings_default_to_the_fixed_loopback_address() -> None:
    settings = Settings.model_validate({})

    assert settings.panel_host == "127.0.0.1"
    assert settings.panel_port == 8766


@pytest.mark.parametrize("host", ("0.0.0.0", "localhost", "192.168.1.10"))
def test_panel_settings_reject_every_non_fixed_loopback_bind_address(host: str) -> None:
    with pytest.raises(ValidationError, match="PANEL_HOST must be 127.0.0.1"):
        Settings.model_validate({"PANEL_HOST": host})


@pytest.mark.parametrize("port", (0, 65536))
def test_panel_settings_reject_out_of_range_port(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"PANEL_PORT": port})


@dataclass
class FakeSettings:
    panel_host: str = "127.0.0.1"
    panel_port: int = 8766
    whoop_client_secret: str = "whoop-client-secret"
    gmail_token_path: str = "gmail-refresh-token"


class FakeServer:
    def __init__(self) -> None:
        self.served = False
        self.closed = False

    def serve_forever(self) -> None:
        self.served = True

    def server_close(self) -> None:
        self.closed = True


def test_panel_serve_starts_injected_server_and_prints_only_loopback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = FakeSettings()
    server = FakeServer()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "build_panel_service",
        lambda actual_settings: captured.setdefault("settings", actual_settings) or object(),
    )
    monkeypatch.setattr(
        cli,
        "serve_panel",
        lambda service, *, host, port: captured.update(
            service=service, host=host, port=port
        ) or server,
    )

    result = CliRunner().invoke(cli.app, ["panel", "serve"])

    assert result.exit_code == 0
    assert result.stdout == "http://127.0.0.1:8766\n"
    assert captured["settings"] is settings
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8766
    assert server.served is True
    assert server.closed is True
    assert settings.whoop_client_secret not in result.stdout
    assert settings.gmail_token_path not in result.stdout
