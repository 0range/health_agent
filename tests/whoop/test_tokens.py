from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from health_agent.whoop.tokens import TokenStore, TokenStoreError, WhoopToken


def make_token(access: str = "access-secret") -> WhoopToken:
    return WhoopToken(
        access_token=access,
        refresh_token="refresh-secret",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("offline", "read:sleep"),
    )


def test_token_is_stored_per_profile_and_account_with_private_modes(
    tmp_path: Path,
) -> None:
    store = TokenStore(tmp_path / "tokens")
    first_token = make_token("first")
    second_token = make_token("second")

    first_path = store.save("vitalii", "main", first_token)
    second_path = store.save("partner", "main", second_token)

    assert store.load("vitalii", "main") == first_token
    assert store.load("partner", "main") == second_token
    assert stat.S_IMODE(first_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    assert first_path != second_path


def test_token_repr_redacts_secrets() -> None:
    rendered = repr(make_token())

    assert "access-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize("value", ("../escape", "A User", "", "a/b"))
def test_profile_and_account_keys_cannot_escape_token_root(
    tmp_path: Path, value: str
) -> None:
    store = TokenStore(tmp_path / "tokens")

    with pytest.raises(TokenStoreError):
        store.save(value, "main", make_token())


def test_symlink_token_is_rejected(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    token_dir = tmp_path / "tokens" / "vitalii"
    token_dir.mkdir(parents=True)
    (token_dir / "main.json").symlink_to(target)

    with pytest.raises(TokenStoreError, match="regular file"):
        store.load("vitalii", "main")


def test_symlink_profile_directory_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    real_store = TokenStore(outside)
    real_store.save("vitalii", "main", make_token())
    linked_root = tmp_path / "tokens"
    linked_root.mkdir()
    (linked_root / "vitalii").symlink_to(outside / "vitalii", target_is_directory=True)

    with pytest.raises(TokenStoreError, match="directory"):
        TokenStore(linked_root).load("vitalii", "main")
