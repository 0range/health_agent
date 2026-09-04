"""Streaming staging importer used until the medical pipeline adapter is wired."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from health_agent.gmail.config import normalize_profile_id, validate_account_id
from health_agent.gmail.types import AttachmentProvenance, ImportReceipt
from health_agent.vault import FileVault


class VaultAttachmentImporter:
    """Stream one attachment to a profile/account-isolated immutable vault."""

    def __init__(
        self,
        profile_id: str,
        account_id: str,
        vault_root: Path,
        temporary_root: Path,
    ) -> None:
        self.profile_id = normalize_profile_id(profile_id)
        self.account_id = validate_account_id(account_id)
        self.vault = FileVault(
            Path(vault_root) / "profiles" / self.profile_id / "gmail" / self.account_id
        )
        self.temporary_root = (
            Path(temporary_root) / self.profile_id / "gmail" / self.account_id
        )

    def import_attachment(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> ImportReceipt:
        if provenance.profile_id != self.profile_id:
            raise ValueError("refusing Gmail attachment for another profile")
        if provenance.account_id != self.account_id:
            raise ValueError("refusing Gmail attachment for another account")
        self.temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temporary_root.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.temporary_root, prefix="gmail-", suffix=".partial"
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("Gmail attachment chunks must be bytes")
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            stored = self.vault.store(temporary)
            if stored.sha256 != digest.hexdigest() or stored.size_bytes != size:
                raise RuntimeError("vault receipt does not match Gmail attachment bytes")
            return ImportReceipt(stored.sha256, size, str(stored.path))
        finally:
            temporary.unlink(missing_ok=True)
