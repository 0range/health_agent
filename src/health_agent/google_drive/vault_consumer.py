"""Streaming adapter from Drive downloads to the existing content-addressed vault."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from health_agent.google_drive.config import validate_profile_id
from health_agent.google_drive.types import ContentReceipt, DriveProvenance
from health_agent.vault import FileVault


class FileVaultDriveConsumer:
    """Consume bounded chunks into a profile-isolated immutable vault."""

    def __init__(self, profile_id: str, vault_root: Path, temporary_root: Path) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.vault = FileVault(Path(vault_root) / "profiles" / self.profile_id)
        self.temporary_root = Path(temporary_root) / self.profile_id

    def consume(
        self, provenance: DriveProvenance, chunks: Iterable[bytes]
    ) -> ContentReceipt:
        if provenance.profile_id != self.profile_id:
            raise ValueError("refusing Drive content for a different profile")
        self.temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temporary_root.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.temporary_root, prefix="drive-", suffix=".partial"
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("Drive gateway chunks must be bytes")
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            stored = self.vault.store(temporary)
            if stored.sha256 != digest.hexdigest() or stored.size_bytes != size:
                raise RuntimeError("vault receipt does not match streamed Drive content")
            return ContentReceipt(stored.sha256, stored.size_bytes, str(stored.path))
        finally:
            temporary.unlink(missing_ok=True)
