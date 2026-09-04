from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from health_agent.config import Settings


def write_credentials(path: Path, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps({"client_id": "client-value", "client_secret": "secret-value"}),
        encoding="utf-8",
    )
    path.chmod(mode)


def test_credentials_file_is_default_when_env_values_are_absent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "whoop-client.json"
    write_credentials(path)

    client_id, secret = Settings(
        whoop_client_credentials_file=path
    ).load_whoop_client_credentials()

    assert client_id == "client-value"
    assert secret.get_secret_value() == "secret-value"


def test_explicit_env_credentials_override_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    client_id, secret = Settings(
        whoop_client_id="override-id",
        whoop_client_secret=SecretStr("override-secret"),
        whoop_client_credentials_file=missing,
    ).load_whoop_client_credentials()

    assert client_id == "override-id"
    assert secret.get_secret_value() == "override-secret"


def test_credentials_file_rejects_insecure_mode_without_secret_in_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "whoop-client.json"
    write_credentials(path, 0o644)

    with pytest.raises(ValueError, match="0600") as caught:
        Settings(whoop_client_credentials_file=path).load_whoop_client_credentials()

    assert "secret-value" not in str(caught.value)


def test_credentials_file_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    linked = tmp_path / "linked.json"
    write_credentials(real)
    linked.symlink_to(real)

    with pytest.raises(ValueError, match="invalid"):
        Settings(whoop_client_credentials_file=linked).load_whoop_client_credentials()
