"""Gmail attachment adapter for the common PostgreSQL medical pipeline."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pymupdf
from sqlalchemy.engine import Engine

from health_agent.db import session_scope
from health_agent.gmail.config import validate_account_id
from health_agent.gmail.types import (
    AttachmentProvenance,
    ImportReceipt,
    PreparedAttachment,
)
from health_agent.importer import import_document
from health_agent.labs import looks_like_lab_document
from health_agent.pdf import extract_pdf
from health_agent.vault import FileVault

_MEDICAL_CONTENT = re.compile(
    r"\b(?:appointment|doctor|physician|clinic|hospital|laboratory|blood|"
    r"analysis|diagnosis|radiology|prescription|при[её]м|врач|клиник|болезн|"
    r"анализ|лаборатор|диагноз|исследован|заключен|рецепт)\w*\b",
    re.IGNORECASE,
)


class MedicalAttachmentImporter:
    """Classify locally, then call the same importer as local PDF uploads."""

    def __init__(
        self, profile_id: str, account_id: str, engine: Engine, vault: FileVault
    ) -> None:
        self.profile_id = str(UUID(profile_id))
        self.account_id = validate_account_id(account_id)
        self.engine = engine
        self.vault = vault
        self.attention_vault = FileVault(
            vault.root
            / "profiles"
            / self.profile_id
            / "gmail-attention"
            / self.account_id
        )

    def import_attachment(
        self, provenance: AttachmentProvenance, prepared: PreparedAttachment
    ) -> ImportReceipt:
        self._require_boundary(provenance)
        if prepared.detected_mime_type != "application/pdf":
            stored = self.attention_vault.store(prepared.path)
            return ImportReceipt(
                stored.sha256,
                stored.size_bytes,
                str(stored.path),
                "ocr_required",
                processing_status="image_ocr_required",
            )

        try:
            if provenance.classification == "ambiguous" and not _pdf_is_medical(
                prepared.path
            ):
                return ImportReceipt(
                    prepared.sha256,
                    prepared.size_bytes,
                    None,
                    "non_medical",
                )

            with session_scope(self.engine) as session:
                report = import_document(
                    session,
                    self.vault,
                    prepared.path,
                    provenance.source_uri,
                    profile_id=UUID(self.profile_id),
                    source_provider="gmail",
                    source_external_id=(
                        f"{provenance.account_id}:{provenance.message_id}:"
                        f"{provenance.part_id}:{provenance.revision}"
                    ),
                )
        except pymupdf.FileDataError:
            stored = self.attention_vault.store(prepared.path)
            return ImportReceipt(
                stored.sha256,
                stored.size_bytes,
                str(stored.path),
                "needs_attention",
                processing_status="invalid_pdf",
            )
        outcome = {
            "imported": "medically_imported",
            "duplicate": "duplicate",
            "ocr_required": "ocr_required",
            "needs_attention": "needs_attention",
        }[report.status]
        return ImportReceipt(
            prepared.sha256,
            prepared.size_bytes,
            str(report.document_id),
            outcome,
            document_id=str(report.document_id),
            processing_status=report.processing_status,
        )

    def _require_boundary(self, provenance: AttachmentProvenance) -> None:
        if provenance.profile_id != self.profile_id:
            raise ValueError("refusing Gmail attachment for another health profile")
        if provenance.account_id != self.account_id:
            raise ValueError("refusing Gmail attachment for another Gmail account")


def _pdf_is_medical(path: Path) -> bool:
    extracted = extract_pdf(path)
    if any(page.extraction_method == "ocr_required" for page in extracted.pages):
        # Keep scanned candidates visible to the shared OCR/attention path.
        return True
    if looks_like_lab_document(extracted.pages):
        return True
    text = "\n".join(page.text for page in extracted.pages)
    return bool(_MEDICAL_CONTENT.search(text))
