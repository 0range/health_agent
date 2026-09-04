from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from time import sleep
from typing import Any

import pytest

from health_agent.whoop.tokens import TokenStore, TokenStoreError, WhoopToken


def make_token(access: str = "access-secret") -> WhoopToken:
    return WhoopToken(
        access_token=access,
        refresh_token="refresh-secret",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("offline", "read:sleep"),
    )


def _rotate_in_process(root: str, counter: Any, start: Any) -> None:
    store = TokenStore(Path(root))
    start.wait()

    def refresh(refresh_token: str) -> WhoopToken:
        assert refresh_token == "single-use-refresh"
        with counter.get_lock():
            counter.value += 1
        sleep(0.05)
        return WhoopToken(
            "rotated-access",
            "rotated-refresh",
            datetime.now(UTC) + timedelta(hours=1),
            ("offline",),
        )

    store.rotate("vitalii", "main", "single-use-refresh", refresh)


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


def test_replacing_restores_prior_good_token_when_caller_fails(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    store.save("vitalii", "main", previous)

    with (
        pytest.raises(RuntimeError, match="database commit"),
        store.replacement("vitalii", "main", make_token("new")) as replacement,
    ):
        replacement.publish()
        raise RuntimeError("database commit failed")

    assert store.load("vitalii", "main") == previous


def test_replacement_is_not_published_until_requested(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    store.save("vitalii", "main", previous)

    with store.replacement("vitalii", "main", make_token("new")):
        pass

    assert store.load("vitalii", "main") == previous


def test_refresh_rotation_is_serialized_across_processes(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    store.save(
        "vitalii",
        "main",
        WhoopToken(
            "expired-access",
            "single-use-refresh",
            datetime.now(UTC) - timedelta(seconds=1),
            ("offline",),
        ),
    )
    context = get_context("spawn")
    counter = context.Value("i", 0)
    start = context.Event()
    processes = [
        context.Process(
            target=_rotate_in_process, args=(str(store.root), counter, start)
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert counter.value == 1
