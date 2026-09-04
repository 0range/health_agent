from __future__ import annotations

import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from health_agent.questions.replies import (
    MAX_REPLY_BYTES,
    REPLY_TTL_SECONDS,
    PrivateReplyStore,
    delivery_request_id,
)
from health_agent.telegram.messenger import OutboundDeliveryConflict
from health_agent.telegram.types import MessageContext

CONTEXT = MessageContext(701, UUID(int=1), 1001, 1001, 88, 701, None, datetime.now(UTC))


def test_private_spool_reopens_exact_bytes_and_rejects_scope_or_content_change(tmp_path: Path):
    root = tmp_path / "replies"
    store = PrivateReplyStore(root)
    reply = "Recorded sleep [SLEEP1].\n\nSources:\nТочное время."
    store.put(CONTEXT, reply)
    path, = root.iterdir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert str(CONTEXT.profile_id) not in path.read_text()
    assert path.name == delivery_request_id(701, 701) + ".reply"
    reopened = PrivateReplyStore(root)
    assert reopened.get(CONTEXT) == reply
    assert reopened.get(replace(CONTEXT, bot_id=702)) is None
    with pytest.raises(OutboundDeliveryConflict):
        reopened.get(replace(CONTEXT, profile_id=uuid4()))
    with pytest.raises(OutboundDeliveryConflict):
        reopened.put(CONTEXT, "Changed reply")
    reopened.complete(701, 701)
    assert list(root.iterdir()) == []


def test_spool_rejects_symlink_directory_and_file_without_reading_target(tmp_path: Path):
    root = tmp_path / "replies"
    store = PrivateReplyStore(root)
    target = tmp_path / "private-target"
    target.write_text("not reply data")
    path = root / (delivery_request_id(701, 701) + ".reply")
    path.symlink_to(target)
    with pytest.raises(OSError):
        store.get(CONTEXT)
    with pytest.raises(OSError):
        store.put(CONTEXT, "reply")
    link = tmp_path / "linked-root"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        PrivateReplyStore(link)
    assert target.read_text() == "not reply data"


def test_spool_rejects_oversize_writes_reads_and_nonprivate_mode(tmp_path: Path):
    store = PrivateReplyStore(tmp_path / "replies")
    with pytest.raises(ValueError):
        store.put(CONTEXT, "x" * (MAX_REPLY_BYTES + 1))
    store.put(CONTEXT, "short reply")
    path, = store.root.iterdir()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        store.get(CONTEXT)
    path.chmod(0o600)
    with path.open("ab") as handle:
        handle.write(b"x" * MAX_REPLY_BYTES)
    with pytest.raises(ValueError):
        store.get(CONTEXT)


def test_orphan_sweep_removes_expired_reply_only_and_ignores_symlinks(tmp_path: Path):
    store = PrivateReplyStore(tmp_path / "replies")
    store.put(CONTEXT, "old reply")
    path, = store.root.iterdir()
    os.utime(path, (1, 1))
    other = store.root / "unrelated"
    other.write_text("leave alone")
    symlink = store.root / ("0" * 64 + ".reply")
    symlink.symlink_to(other)
    PrivateReplyStore(store.root, clock=lambda: REPLY_TTL_SECONDS + 2)
    assert not path.exists()
    assert other.exists() and symlink.is_symlink()
