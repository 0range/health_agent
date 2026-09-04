from __future__ import annotations

import json
import multiprocessing
import stat
from dataclasses import asdict
from pathlib import Path
from queue import Empty

import pytest

from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.stores import (
    GmailBindingConflict,
    LocalGmailProfileStore,
    LocalGmailStateStore,
    LocalGmailTokenStore,
)
from health_agent.gmail.types import SeenAttachment, SeenMessage

PROFILE_A = "11111111-1111-1111-1111-111111111111"
PROFILE_B = "22222222-2222-2222-2222-222222222222"


def seen_message(profile_id: str, account_id: str) -> SeenMessage:
    return SeenMessage(
        profile_id, account_id, "message-1", "15", 1000, "ambiguous", "processed"
    )


def seen_attachment(profile_id: str, account_id: str) -> SeenAttachment:
    return SeenAttachment(
        profile_id,
        account_id,
        "message-1",
        "part-1",
        "message-1:part-1:attachment-1",
        "attachment-1",
        "labs.pdf",
        "application/pdf",
        "suspected_medical",
        10,
        "a" * 64,
        10,
        "vault/ref",
        "processed",
        outcome="medically_imported",
    )


def test_files_are_private_and_state_is_profile_account_isolated(
    tmp_path: Path,
) -> None:
    profiles = LocalGmailProfileStore(tmp_path)
    tokens = LocalGmailTokenStore(tmp_path)
    state = LocalGmailStateStore(tmp_path)
    profile = GmailProfile.empty(PROFILE_A).upsert_account(GmailAccount.create("one"))
    profile = profile.upsert_account(GmailAccount.create("two"))
    profiles.save(profile)
    token = tokens.publish_verified(
        PROFILE_A, "one", "one@example.com", json.dumps({"token": "secret"})
    )
    state.record_message(seen_message(PROFILE_A, "one"))
    state.record_attachment(seen_attachment(PROFILE_A, "one"))
    state.set_cursor(PROFILE_A, "one", "20")

    assert state.get_cursor(PROFILE_A, "one") == "20"
    assert state.get_cursor(PROFILE_A, "two") is None
    assert state.get_cursor(PROFILE_B, "one") is None
    assert state.get_message(PROFILE_A, "two", "message-1") is None
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / PROFILE_A / "profile.json").stat().st_mode) == 0o600


def test_store_rejects_payload_copied_across_account_boundary(tmp_path: Path) -> None:
    path = tmp_path / PROFILE_A / "accounts" / "two" / "sync-state.json"
    path.parent.mkdir(parents=True)
    message = seen_message(PROFILE_A, "one")
    path.write_text(
        json.dumps(
            {
                "history_id": None,
                "messages": {message.message_id: asdict(message)},
                "attachments": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="another account"):
        LocalGmailStateStore(tmp_path).get_message(PROFILE_A, "two", "message-1")


def test_removal_marks_message_and_all_attachments(tmp_path: Path) -> None:
    state = LocalGmailStateStore(tmp_path)
    state.record_message(seen_message(PROFILE_A, "one"))
    state.record_attachment(seen_attachment(PROFILE_A, "one"))
    assert state.mark_message_removed(PROFILE_A, "one", "message-1") == 2
    assert state.get_message(PROFILE_A, "one", "message-1").status == "removed"  # type: ignore[union-attr]
    assert (
        state.get_attachment(
            PROFILE_A,
            "one",
            "message-1",
            "part-1",
            "message-1:part-1:attachment-1",
        ).status
        == "removed"
    )  # type: ignore[union-attr]
    assert state.mark_message_removed(PROFILE_A, "one", "message-1") == 0


def test_verified_mailbox_cannot_cross_health_profile(tmp_path: Path) -> None:
    tokens = LocalGmailTokenStore(tmp_path)
    tokens.publish_verified(PROFILE_A, "one", "same@example.com", "{}")
    with pytest.raises(GmailBindingConflict):
        tokens.publish_verified(PROFILE_B, "one", "same@example.com", "{}")


def test_profile_directory_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / PROFILE_A).symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinked"):
        LocalGmailStateStore(tmp_path).get_cursor(PROFILE_A, "one")


def _wait_for_account_lock(root: str, queue: multiprocessing.Queue[str]) -> None:
    state = LocalGmailStateStore(Path(root))
    queue.put("ready")
    with state.sync_lock(PROFILE_A, "one"):
        queue.put("acquired")
        state.record_message(
            SeenMessage(
                PROFILE_A,
                "one",
                "message-2",
                "20",
                2000,
                "appointment",
                "attention",
            )
        )
        state.set_cursor(PROFILE_A, "one", "20")


def test_sync_lock_serializes_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    queue: multiprocessing.Queue[str] = context.Queue()
    state = LocalGmailStateStore(tmp_path)
    with state.sync_lock(PROFILE_A, "one"):
        process = context.Process(
            target=_wait_for_account_lock, args=(str(tmp_path), queue)
        )
        process.start()
        assert queue.get(timeout=2) == "ready"
        with pytest.raises(Empty):
            queue.get(timeout=0.2)
        state.record_message(seen_message(PROFILE_A, "one"))
        state.set_cursor(PROFILE_A, "one", "10")
    assert queue.get(timeout=2) == "acquired"
    process.join(timeout=2)
    assert process.exitcode == 0
    assert state.get_message(PROFILE_A, "one", "message-1") is not None
    assert state.get_message(PROFILE_A, "one", "message-2") is not None
    assert state.get_cursor(PROFILE_A, "one") == "20"
