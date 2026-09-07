"""Bounded offline salvage of an already-spent, explicitly failed cloud response."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import Engine, select

from health_agent.ai.yandex import _chat_content
from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionJob
from health_agent.lab_extraction.queue import (
    _finish,
    _insert_candidates,
    _refresh_after_insertion,
    profile_lock,
)
from health_agent.lab_extraction.types import (
    EXTRACTOR_VERSION,
    MAX_CLOUD_CHARACTERS,
    ExtractionError,
    PartialExtraction,
)
from health_agent.lab_extraction.validation import validate_partial_candidates
from health_agent.models import Document, DocumentPage

MAX_CAPTURE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CachedImportReport:
    inserted: int
    accepted: int
    rejected: int


def read_capture(path: Path) -> tuple[str, PartialExtraction]:
    """Read a regular capture once, without provider setup or credential access."""
    try:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ExtractionError("unsafe_extraction_path")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as capture:
            metadata = os.fstat(capture.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ExtractionError("unsafe_extraction_path")
            if metadata.st_size > MAX_CAPTURE_BYTES:
                raise ExtractionError("cloud_invalid_output")
            raw = capture.read(MAX_CAPTURE_BYTES + 1)
        if len(raw) > MAX_CAPTURE_BYTES:
            raise ExtractionError("cloud_invalid_output")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ExtractionError("cloud_invalid_output")
        text = payload.get("page_text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_CLOUD_CHARACTERS
        ):
            raise ExtractionError("cloud_input_limit")
        text.encode("utf-8")
        response = json.loads(
            json.dumps(payload.get("response")),
            object_hook=lambda value: SimpleNamespace(**value),
        )
        content = _chat_content(response)
        result = validate_partial_candidates(json.loads(content), text)
        return text, result
    except ExtractionError:
        raise
    except (OSError, TypeError, ValueError, RecursionError):
        raise ExtractionError("cloud_invalid_output") from None


def import_cached(
    engine: Engine,
    profile_id: UUID,
    document_id: UUID,
    page_number: int,
    capture_path: Path,
) -> CachedImportReport:
    source_text, partial = read_capture(capture_path)
    digest = hashlib.sha256(source_text.encode()).hexdigest()
    with profile_lock(engine, profile_id), session_scope(engine) as session:
        document = session.scalar(
            select(Document)
            .where(
                Document.id == document_id,
                Document.profile_id == profile_id,
            )
            .with_for_update()
        )
        if document is None:
            raise ExtractionError("document_not_found")
        page = session.scalar(
            select(DocumentPage)
            .where(
                DocumentPage.document_id == document_id,
                DocumentPage.page_number == page_number,
            )
            .with_for_update()
        )
        jobs = session.scalars(
            select(LabExtractionJob)
            .where(
                LabExtractionJob.document_id == document_id,
                LabExtractionJob.page_number == page_number,
            )
            .with_for_update()
        ).all()
        job = next(
            (
                row
                for row in jobs
                if row.extractor_version == EXTRACTOR_VERSION
                and row.profile_id == profile_id
            ),
            None,
        )
        if (
            page is None
            or job is None
            or job.status != "needs_attention"
            or job.safe_error_code
            not in {"cloud_invalid_output", "cloud_partial_output"}
            or job.cloud_attempts < 1
            or not job.local_completed
            or job.claim_token is not None
            or any(
                row.status == "cloud_in_flight"
                or row.safe_error_code == "cloud_outcome_unknown"
                for row in jobs
            )
        ):
            raise ExtractionError("cached_import_invalid_state")
        if page.extracted_text != source_text or (
            job.source_text_sha256 is not None and job.source_text_sha256 != digest
        ):
            raise ExtractionError("page_evidence_changed")
        inserted = _insert_candidates(
            session,
            document_id,
            page_number,
            partial.candidates,
            cloud=True,
            reason_code="lab_extraction_v3_cached",
        )
        job.candidate_count += inserted
        job.extraction_method = "cached_structured_partial_v3"
        _finish(job, "needs_attention", "cloud_partial_output")
        if inserted:
            _refresh_after_insertion(session, document)
        return CachedImportReport(
            inserted, len(partial.candidates), partial.rejected_count
        )
