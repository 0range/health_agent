from __future__ import annotations

from pathlib import Path

import pytest

from health_agent.config import Settings


def _write_key(path: Path, content: str = "file-key", mode: int = 0o600) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def test_openai_environment_key_takes_precedence_over_private_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key"
    _write_key(key_file, "file-key", 0o644)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")

    key = Settings(openai_api_key_file=key_file).load_openai_api_key()

    assert key.get_secret_value() == "environment-key"


def test_openai_private_regular_0600_file_is_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    key_file = tmp_path / "key"
    _write_key(key_file)

    assert (
        Settings(openai_api_key_file=key_file).load_openai_api_key().get_secret_value()
        == "file-key"
    )


@pytest.mark.parametrize("mode", (0o644, 0o400, 0o700))
def test_openai_private_file_requires_exact_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: int
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    key_file = tmp_path / "key"
    _write_key(key_file, "very-private-key", mode)

    with pytest.raises(ValueError, match="0600") as caught:
        Settings(openai_api_key_file=key_file).load_openai_api_key()

    assert "very-private-key" not in str(caught.value)


def test_openai_private_file_rejects_symlink_without_following_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    target = tmp_path / "target"
    key_file = tmp_path / "key"
    _write_key(target, "private-target-key")
    key_file.symlink_to(target)

    with pytest.raises(ValueError, match="invalid") as caught:
        Settings(openai_api_key_file=key_file).load_openai_api_key()

    assert "private-target-key" not in str(caught.value)


def test_openai_missing_or_empty_key_has_only_a_safe_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    key_file = tmp_path / "key"
    _write_key(key_file, "super-secret-empty-file-marker")
    key_file.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid") as caught:
        Settings(openai_api_key_file=key_file).load_openai_api_key()

    assert "super-secret-empty-file-marker" not in str(caught.value)
