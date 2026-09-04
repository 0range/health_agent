from __future__ import annotations

import os
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

    store.recover("vitalii", "main", None)
    assert store.load("vitalii", "main") == previous


def test_replacement_is_not_published_until_requested(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    store.save("vitalii", "main", previous)

    with store.replacement("vitalii", "main", make_token("new")):
        pass

    assert store.load("vitalii", "main") == previous


def test_committed_replacement_keeps_candidate_and_clears_journal(
    tmp_path: Path,
) -> None:
    store = TokenStore(tmp_path / "tokens")
    store.save("vitalii", "main", make_token("previous"))
    candidate = make_token("candidate")

    with store.replacement("vitalii", "main", candidate) as replacement:
        replacement.publish()
        replacement.resolve(replacement.generation)
        replacement.resolve(replacement.generation)

    assert store.load("vitalii", "main") == candidate
    assert not (store.root / "vitalii" / "main.journal").exists()


@pytest.mark.parametrize("database_committed", (False, True))
def test_interrupted_replacement_recovers_from_database_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_committed: bool
) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    candidate = make_token("candidate")
    store.save("vitalii", "main", previous)
    context = store.replacement("vitalii", "main", candidate)
    replacement = context.__enter__()
    replacement.publish()
    monkeypatch.setattr(replacement, "rollback", lambda: None)
    context.__exit__(None, None, None)
    journal_path = store.root / "vitalii" / "main.journal"
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600

    recovered = TokenStore(store.root)
    recovered.recover(
        "vitalii",
        "main",
        replacement.generation if database_committed else None,
    )

    assert recovered.load("vitalii", "main") == (
        candidate if database_committed else previous
    )


def test_interrupt_after_database_commit_keeps_journal_for_restart(
    tmp_path: Path,
) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    candidate = make_token("candidate")
    store.save("vitalii", "main", previous)
    generation = None

    with (
        pytest.raises(KeyboardInterrupt),
        store.replacement("vitalii", "main", candidate) as replacement,
    ):
        generation = replacement.generation
        replacement.publish()
        raise KeyboardInterrupt

    assert generation is not None
    restarted = TokenStore(store.root)
    restarted.recover("vitalii", "main", generation)
    assert restarted.load("vitalii", "main") == candidate


@pytest.mark.parametrize(
    "fault_stage",
    (
        "token_fchmod",
        "token_fsync",
        "token_replace",
        "token_directory_fsync",
        "journal_cleanup",
        "journal_directory_fsync",
    ),
)
def test_every_post_commit_finalization_fault_is_restart_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    store = TokenStore(tmp_path / "tokens")
    candidate = make_token("candidate")
    store.save("vitalii", "main", make_token("previous"))
    generation = None

    with (
        pytest.raises(TokenStoreError),
        store.replacement("vitalii", "main", candidate) as replacement,
    ):
        generation = replacement.generation
        replacement.publish()
        with monkeypatch.context() as fault:
            if fault_stage == "token_fchmod":
                fault.setattr(
                    os,
                    "fchmod",
                    lambda *args: (_ for _ in ()).throw(OSError("synthetic fchmod")),
                )
            elif fault_stage == "token_fsync":
                fault.setattr(
                    os,
                    "fsync",
                    lambda *args: (_ for _ in ()).throw(OSError("synthetic fsync")),
                )
            elif fault_stage == "token_replace":
                fault.setattr(
                    os,
                    "replace",
                    lambda *args: (_ for _ in ()).throw(OSError("synthetic replace")),
                )
            elif fault_stage == "token_directory_fsync":
                fault.setattr(
                    store,
                    "_sync_directory",
                    lambda *args: (_ for _ in ()).throw(
                        OSError("synthetic token directory fsync")
                    ),
                )
            elif fault_stage == "journal_cleanup":
                fault.setattr(
                    store,
                    "_clear_journal_unlocked",
                    lambda *args: (_ for _ in ()).throw(
                        TokenStoreError("synthetic journal cleanup")
                    ),
                )
            else:
                sync_calls = 0
                original_sync = store._sync_directory

                def fail_journal_directory_sync(path: Path) -> None:
                    nonlocal sync_calls
                    sync_calls += 1
                    if sync_calls == 2:
                        raise OSError("synthetic journal directory fsync")
                    original_sync(path)

                fault.setattr(store, "_sync_directory", fail_journal_directory_sync)
            replacement.resolve(replacement.generation)

    assert generation is not None
    restarted = TokenStore(store.root)
    restarted.recover("vitalii", "main", generation)
    assert restarted.load("vitalii", "main") == candidate


def test_post_replace_fsync_failure_restores_previous_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    store.save("vitalii", "main", previous)
    original_sync = store._sync_directory
    sync_calls = 0

    def fail_candidate_sync(path: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError("synthetic post-replace failure")
        original_sync(path)

    monkeypatch.setattr(store, "_sync_directory", fail_candidate_sync)

    with (
        pytest.raises(TokenStoreError, match="storage is unavailable"),
        store.replacement("vitalii", "main", make_token("candidate")) as replacement,
    ):
        replacement.publish()

    store.recover("vitalii", "main", None)
    assert store.load("vitalii", "main") == previous


def test_interrupted_standalone_save_recovers_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = make_token("previous")
    candidate = make_token("candidate")
    store.save("vitalii", "main", previous)
    original_clear = store._clear_journal_unlocked
    calls = 0

    def fail_first_clear(profile: str, account: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TokenStoreError("synthetic interrupted save")
        original_clear(profile, account)

    monkeypatch.setattr(store, "_clear_journal_unlocked", fail_first_clear)
    with pytest.raises(TokenStoreError, match="interrupted"):
        store.save("vitalii", "main", candidate)
    monkeypatch.setattr(store, "_clear_journal_unlocked", original_clear)

    assert TokenStore(store.root).load("vitalii", "main") == candidate


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
