"""Optional vault-only adapter for tests and non-medical staging workflows."""

from __future__ import annotations

from pathlib import Path

from health_agent.gmail.config import normalize_profile_id, validate_account_id
from health_agent.gmail.types import (
    AttachmentProvenance,
    ImportReceipt,
    PreparedAttachment,
)
from health_agent.vault import FileVault


class VaultAttachmentImporter:
    """Copy one prepared attachment to an isolated immutable vault."""

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
        self, provenance: AttachmentProvenance, prepared: PreparedAttachment
    ) -> ImportReceipt:
        if provenance.profile_id != self.profile_id:
            raise ValueError("refusing Gmail attachment for another profile")
        if provenance.account_id != self.account_id:
            raise ValueError("refusing Gmail attachment for another account")
        stored = self.vault.store(prepared.path)
        if stored.sha256 != prepared.sha256 or stored.size_bytes != prepared.size_bytes:
            raise RuntimeError("vault receipt does not match Gmail attachment bytes")
        return ImportReceipt(
            stored.sha256,
            stored.size_bytes,
            str(stored.path),
            "staged",
        )
