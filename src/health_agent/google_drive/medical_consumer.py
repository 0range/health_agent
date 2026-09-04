"""Stream Drive content into the shared medical database/import/review pipeline."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

import pymupdf
from sqlalchemy import select
from sqlalchemy.engine import Engine

from health_agent.db import session_scope
from health_agent.google_drive.config import validate_profile_id
from health_agent.google_drive.types import (
    ContentReceipt,
    DriveProvenance,
    GlobalDriveSyncError,
)
from health_agent.importer import import_document
from health_agent.models import Profile
from health_agent.vault import FileVault


class MedicalDriveConsumer:
    """Persist PDFs to PostgreSQL and route images/corrupt PDFs to attention."""

    def __init__(
        self,
        profile_id: str,
        engine: Engine,
        vault: FileVault,
        temporary_root: Path,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.profile_uuid = UUID(self.profile_id)
        self.engine = engine
        self.vault = vault
        self.attention_vault = FileVault(
            vault.root / "profiles" / self.profile_id / "drive-attention"
        )
        self.temporary_root = Path(temporary_root) / self.profile_id / "drive"

    def consume(
        self, provenance: DriveProvenance, chunks: Iterable[bytes]
    ) -> ContentReceipt:
        if provenance.profile_id != self.profile_id:
            raise ValueError("refusing Drive content for another health profile")
        self._require_database_profile()
        temporary, sha256, size = self._stage(chunks)
        try:
            if provenance.output_media_type != "application/pdf":
                stored = self.attention_vault.store(temporary)
                return ContentReceipt(
                    sha256,
                    size,
                    str(stored.path),
                    outcome="ocr_required",
                    processing_status="image_ocr_required",
                )
            try:
                with session_scope(self.engine) as session:
                    report = import_document(
                        session,
                        self.vault,
                        temporary,
                        provenance.item.web_view_link
                        or f"https://drive.google.com/file/d/{provenance.item.file_id}/view",
                        profile_id=self.profile_uuid,
                        source_provider="google_drive",
                        source_external_id=provenance.item.file_id,
                        source_revision=provenance.item.revision or f"sha256:{sha256}",
                    )
            except pymupdf.FileDataError:
                stored = self.attention_vault.store(temporary)
                return ContentReceipt(
                    sha256,
                    size,
                    str(stored.path),
                    outcome="needs_attention",
                    processing_status="invalid_pdf",
                )
            outcome = {
                "imported": "medically_imported",
                "duplicate": "duplicate",
                "ocr_required": "ocr_required",
                "needs_attention": "needs_attention",
            }[report.status]
            return ContentReceipt(
                sha256,
                size,
                str(report.document_id),
                outcome=outcome,
                processing_status=report.processing_status,
                document_id=str(report.document_id),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _require_database_profile(self) -> None:
        with session_scope(self.engine) as session:
            if session.scalar(select(Profile.id).where(Profile.id == self.profile_uuid)) is None:
                raise GlobalDriveSyncError(
                    "Drive profile does not exist in the health database"
                )

    def _stage(self, chunks: Iterable[bytes]) -> tuple[Path, str, int]:
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
            return temporary, digest.hexdigest(), size
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
