"""Private, short-lived delivery replies; never a dialogue or retrieval cache."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from health_agent.telegram.messenger import OutboundDeliveryConflict
from health_agent.telegram.stores import private_directory
from health_agent.telegram.types import MessageContext

MAX_REPLY_BYTES = 128 * 1024
REPLY_TTL_SECONDS = 7 * 24 * 60 * 60
_REPLY_NAME = re.compile(r"[0-9a-f]{64}\.reply")


def delivery_request_id(bot_id: int, update_id: int) -> str:
    return hashlib.sha256(
        f"health-agent-telegram-reply-v1:{bot_id}:{update_id}".encode("ascii")
    ).hexdigest()


def _scope(context: MessageContext) -> str:
    return hashlib.sha256(
        f"{context.profile_id}:{context.telegram_user_id}:{context.chat_id}".encode("ascii")
    ).hexdigest()


class PrivateReplyStore:
    """Atomic 0600 reply files in a 0700 directory, removed after terminal delivery.

    Only opaque scope hashes and final reply bytes are stored. The content-free
    Telegram audit remains the authority for part hashes and unknown deliveries.
    """

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = private_directory(root)
        self._clock = clock
        self.sweep()

    def get(self, context: MessageContext) -> str | None:
        private_directory(self.root)
        path = self._path(context.bot_id, context.update_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_REPLY_BYTES + 65
            ):
                raise ValueError("prepared reply is invalid")
            data = handle.read(MAX_REPLY_BYTES + 66)
        if len(data) > MAX_REPLY_BYTES + 65:
            raise ValueError("prepared reply is invalid")
        scope, separator, text = data.partition(b"\n")
        if scope != _scope(context).encode("ascii") or not separator:
            raise OutboundDeliveryConflict()
        result = text.decode("utf-8")
        if not result.strip():
            raise ValueError("prepared reply is invalid")
        return result

    def put(self, context: MessageContext, text: str) -> str:
        payload = text.encode("utf-8")
        if not text.strip() or len(payload) > MAX_REPLY_BYTES:
            raise ValueError("prepared reply exceeds the delivery bound")
        private_directory(self.root)
        existing = self.get(context)
        if existing is not None:
            if existing != text:
                raise OutboundDeliveryConflict()
            return existing
        descriptor, name = tempfile.mkstemp(dir=self.root, prefix=".reply-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(_scope(context).encode("ascii") + b"\n" + payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Publish without overwriting a competing worker's prepared bytes.
                os.link(temporary, self._path(context.bot_id, context.update_id))
            except FileExistsError:
                if self.get(context) != text:
                    raise OutboundDeliveryConflict() from None
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)
        return text

    def complete(self, bot_id: int, update_id: int) -> None:
        private_directory(self.root)
        path = self._path(bot_id, update_id)
        if path.is_symlink():
            raise ValueError("prepared reply is invalid")
        path.unlink(missing_ok=True)
        self._sync_directory()

    def sweep(self) -> None:
        """Remove only expired regular spool files; never follow symlinks."""

        private_directory(self.root)
        cutoff = self._clock() - REPLY_TTL_SECONDS
        for path in self.root.iterdir():
            if not (_REPLY_NAME.fullmatch(path.name) or path.name.startswith(".reply-")):
                continue
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < cutoff:
                path.unlink()

    def _path(self, bot_id: int, update_id: int) -> Path:
        return self.root / f"{delivery_request_id(bot_id, update_id)}.reply"

    def _sync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
