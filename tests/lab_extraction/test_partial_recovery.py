import json

import pytest
from sqlalchemy import select

from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionJob, LabExtractionProfile
from health_agent.lab_extraction.openai import OpenAILabExtractor
from health_agent.lab_extraction.types import ExtractionError
from health_agent.lab_extraction.validation import validate_partial_candidates
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus
from lab_extraction.test_openai import Client, FakeSettings
from lab_extraction.test_service import Cloud, add_page, service


def capture(tmp_path, *, text, rows=None, response=None):
    path = tmp_path / "capture.json"
    path.write_text(
        json.dumps(
            {
                "page_text": text,
                "response": response
                or {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"candidates": rows}),
                            },
                        }
                    ]
                },
            }
        )
    )
    return path


GOOD = {
    "source_name": "Glucose",
    "source_value": "5.1",
    "source_unit": "mmol/L",
    "reference_text": "3.9 - 6.1",
    "source_flag": None,
    "evidence_excerpt": "Glucose 5.1 mmol/L 3.9 - 6.1",
}
BAD = {**GOOD, "source_value": "99"}
TEXT = GOOD["evidence_excerpt"]


@pytest.mark.parametrize(
    "rows,accepted,rejected", [([GOOD, BAD], 1, 1), ([BAD], 0, 1), ([], 0, 0)]
)
def test_partial_validation(rows, accepted, rejected):
    result = validate_partial_candidates({"candidates": rows}, TEXT)
    assert len(result.candidates) == accepted
    assert result.rejected_count == rejected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"candidates": {}},
        {"candidates": [GOOD] * 41},
        {"candidates": [], "extra": 1},
    ],
)
def test_partial_outer_rejected(payload):
    with pytest.raises(ValueError):
        validate_partial_candidates(payload, TEXT)


def test_openai_partial_one_response_and_strict_unchanged():
    client = Client(text=json.dumps({"candidates": [GOOD, BAD]}))
    adapter = OpenAILabExtractor(FakeSettings(), client=client)
    result = adapter.extract_partial(DEFAULT_PROFILE_ID, TEXT)
    assert len(result.candidates) == result.rejected_count == 1
    assert len(client.responses.calls) == 1
    with pytest.raises(ExtractionError, match="cloud_invalid_output"):
        adapter.extract(DEFAULT_PROFILE_ID, TEXT)


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "directory",
        "oversize",
        "text_limit",
        "output_limit",
        "truncated",
        "refusal",
        "tool",
        "unknown",
        "malformed",
    ],
)
def test_capture_rejects_unsafe_file_or_envelope(tmp_path, kind):
    from health_agent.lab_extraction.cached import read_capture

    path = capture(tmp_path, text=TEXT, rows=[GOOD])
    if kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif kind == "directory":
        path = tmp_path
    elif kind == "oversize":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    elif kind == "text_limit":
        path = capture(tmp_path, text="a" * 12001, rows=[])
    elif kind == "malformed":
        path.write_text("{")
    else:
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"candidates": [GOOD]}),
                    },
                }
            ]
        }
        choice = response["choices"][0]
        if kind == "output_limit":
            choice["message"]["content"] = " " * 80001
        if kind == "truncated":
            choice["finish_reason"] = "length"
        if kind == "unknown":
            choice["finish_reason"] = "mystery"
        if kind == "refusal":
            choice["message"]["refusal"] = "no"
        if kind == "tool":
            choice["message"]["tool_calls"] = [{}]
        path = capture(tmp_path, text=TEXT, response=response)
    with pytest.raises(ExtractionError):
        read_capture(path)


def test_cached_cli_never_initializes_provider(clean_database, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from health_agent.config import Settings
    from health_agent.lab_extraction import cli

    document, _worker = failed_page(clean_database, tmp_path)
    path = capture(tmp_path, text=TEXT, rows=[GOOD, BAD])
    monkeypatch.setattr(
        cli, "Settings", lambda: Settings(_env_file=None, ai_provider="yandex")
    )
    monkeypatch.setattr(cli, "build_engine", lambda settings: clean_database)
    monkeypatch.setattr(
        cli, "build_service", lambda: pytest.fail("must not construct provider")
    )
    result = CliRunner().invoke(
        cli.app,
        ["import-cached", str(DEFAULT_PROFILE_ID), str(document), "1", str(path)],
    )
    assert result.exit_code == 0, result.output
    assert result.output == "inserted=1 accepted=1 rejected=1\n"


@pytest.mark.parametrize("rows,rejected", [([], 0), ([BAD], 1)])
def test_cloud_empty_and_all_invalid_states(clean_database, tmp_path, rows, rejected):
    class PartialCloud:
        def extract_partial(self, profile_id, text):
            return validate_partial_candidates({"candidates": rows}, text)

    add_page(clean_database, "Glucose\n5.1 mmol/L 3.9 - 6.1")
    worker = service(clean_database, tmp_path, cloud=PartialCloud())
    worker.configure(DEFAULT_PROFILE_ID, cloud=True)
    report = worker.run(DEFAULT_PROFILE_ID)
    assert (report.inserted, report.cloud_requests, report.attention) == (
        0,
        1,
        rejected,
    )
    with session_scope(clean_database) as session:
        job = session.scalars(select(LabExtractionJob)).one()
        assert job.status == ("needs_attention" if rejected else "completed")


@pytest.mark.parametrize("status,refusal", [("incomplete", False), ("completed", True)])
def test_openai_partial_invalid_envelope(status, refusal):
    client = Client(status=status, refusal=refusal)
    with pytest.raises(ExtractionError):
        OpenAILabExtractor(FakeSettings(), client=client).extract_partial(
            DEFAULT_PROFILE_ID, TEXT
        )
    assert len(client.calls) == 1


def test_cached_limit_rolls_back_earlier_inserts_across_versions(
    clean_database, tmp_path
):
    import hashlib

    from health_agent.lab_extraction.cached import import_cached
    from health_agent.models import DocumentPage

    document, _worker = failed_page(clean_database, tmp_path)
    second = {
        **GOOD,
        "source_name": "Other assay",
        "evidence_excerpt": TEXT.replace("Glucose", "Other assay"),
    }
    full_text = TEXT + "\n" + second["evidence_excerpt"]
    with session_scope(clean_database) as session:
        job = session.scalars(select(LabExtractionJob)).one()
        job.source_text_sha256 = hashlib.sha256(full_text.encode()).hexdigest()
        page = session.scalars(select(DocumentPage)).one()
        page.extracted_text = full_text
        session.add(
            LabExtractionJob(
                profile_id=DEFAULT_PROFILE_ID,
                document_id=document,
                page_number=1,
                extractor_version="historical-version",
                status="completed",
                candidate_count=39,
            )
        )
    path = capture(tmp_path, text=full_text, rows=[GOOD, second])
    with pytest.raises(ExtractionError, match="candidate_limit"):
        import_cached(clean_database, DEFAULT_PROFILE_ID, document, 1, path)
    with session_scope(clean_database) as session:
        assert session.scalars(select(LabObservation)).all() == []
        job = session.scalars(
            select(LabExtractionJob).where(
                LabExtractionJob.extractor_version == "lab-extraction-v1"
            )
        ).one()
        assert job.candidate_count == 0
        assert job.safe_error_code == "cloud_invalid_output"


def test_cached_limit_counts_nonqueue_observations(clean_database, tmp_path):
    from health_agent.lab_extraction.cached import import_cached

    document, _worker = failed_page(clean_database, tmp_path)
    with session_scope(clean_database) as session:
        for index in range(40):
            session.add(
                LabObservation(
                    document_id=document,
                    page_number=1,
                    canonical_name=f"historical_{index}",
                    source_name=f"Historical {index}",
                    source_value="1",
                    source_unit="U/L",
                    evidence_excerpt="Historical source",
                    confidence=0.5,
                    status=ReviewStatus.REJECTED,
                )
            )
    path = capture(tmp_path, text=TEXT, rows=[GOOD])
    with pytest.raises(ExtractionError, match="candidate_limit"):
        import_cached(clean_database, DEFAULT_PROFILE_ID, document, 1, path)
    with session_scope(clean_database) as session:
        assert len(session.scalars(select(LabObservation)).all()) == 40
        assert session.scalars(select(LabExtractionJob)).one().candidate_count == 0


@pytest.mark.parametrize("safe_error", [None, "vault_integrity"])
def test_cached_new_pending_refreshes_document_preserving_verified_sibling(
    clean_database,
    tmp_path,
    safe_error,
):
    from datetime import date
    from decimal import Decimal

    from health_agent.lab_extraction.cached import import_cached
    from health_agent.models import Document, DocumentPage

    document_id, _worker = failed_page(clean_database, tmp_path)
    with session_scope(clean_database) as session:
        document = session.get_one(Document, document_id)
        document.processing_status = (
            "processed" if safe_error is None else "needs_attention"
        )
        document.safe_error_code = safe_error
        document.collected_date = date(2024, 1, 2)
        sibling = LabObservation(
            document_id=document_id,
            page_number=1,
            canonical_name="historical",
            source_name="Historical assay",
            source_value="2",
            source_unit="U/L",
            parsed_value=Decimal(2),
            normalized_value=Decimal(2),
            normalized_unit="U/L",
            evidence_excerpt="Historical assay 2 U/L",
            confidence=1,
            status=ReviewStatus.VERIFIED,
        )
        session.add(sibling)
        session.flush()
        sibling_id = sibling.id
    path = capture(tmp_path, text=TEXT, rows=[GOOD, BAD])
    assert (
        import_cached(clean_database, DEFAULT_PROFILE_ID, document_id, 1, path).inserted
        == 1
    )
    assert (
        import_cached(clean_database, DEFAULT_PROFILE_ID, document_id, 1, path).inserted
        == 0
    )
    with session_scope(clean_database) as session:
        document = session.get_one(Document, document_id)
        assert document.processing_status == (
            "needs_review" if safe_error is None else "needs_attention"
        )
        assert document.safe_error_code == safe_error
        assert document.collected_date == date(2024, 1, 2)
        sibling = session.get_one(LabObservation, sibling_id)
        assert (
            sibling.status,
            sibling.source_name,
            sibling.source_value,
            sibling.parsed_value,
        ) == (ReviewStatus.VERIFIED, "Historical assay", "2", Decimal(2))
        pending = session.scalars(
            select(LabObservation).where(LabObservation.id != sibling_id)
        ).one()
        assert pending.status == ReviewStatus.NEEDS_REVIEW
        job = session.scalars(select(LabExtractionJob)).one()
        assert (job.status, job.safe_error_code, job.cloud_attempts) == (
            "needs_attention",
            "cloud_partial_output",
            1,
        )
        assert session.scalars(select(DocumentPage)).one().extracted_text == TEXT


def test_partial_restores_only_name_whitespace_per_row():
    text = "Assay monomeric (post\nPEG)\n137\nmIU/L\n72 - 229"
    good = {
        **GOOD,
        "source_name": "Assay monomeric (post PEG)",
        "source_value": "137",
        "source_unit": "mIU/L",
        "reference_text": "72 - 229",
        "evidence_excerpt": text,
    }
    result = validate_partial_candidates(
        {"candidates": [good, {**good, "source_value": "138"}]}, text
    )
    assert result.rejected_count == 1
    assert result.candidates[0].source_name == "Assay monomeric (post\nPEG)"


def failed_page(engine, tmp_path):
    document = add_page(engine, TEXT)
    worker = service(
        engine, tmp_path, cloud=Cloud(error=ExtractionError("cloud_invalid_output"))
    )
    worker.configure(DEFAULT_PROFILE_ID, cloud=True)
    worker.queue.discover_and_recover(DEFAULT_PROFILE_ID)
    job_id = worker.queue.pending(DEFAULT_PROFILE_ID, 1, cloud=True)[0]
    claim = worker.queue.claim(DEFAULT_PROFILE_ID, job_id)
    worker.queue.publish(claim, TEXT, (), cloud=False, unresolved=True)
    assert worker.queue.reserve_cloud(
        claim, worker.clock().date(), "original-model", allowed=True
    )
    worker.queue.fail(claim, "cloud_invalid_output")
    return document, worker


def test_cloud_partial_publish_keeps_attention(clean_database, tmp_path):
    class PartialCloud:
        def extract_partial(self, profile_id, text):
            return validate_partial_candidates({"candidates": [GOOD, BAD]}, text)

    add_page(clean_database, TEXT + "\nUnknown marker 2 U/L")
    worker = service(clean_database, tmp_path, cloud=PartialCloud())
    worker.configure(DEFAULT_PROFILE_ID, cloud=True)
    report = worker.run(DEFAULT_PROFILE_ID)
    assert (report.inserted, report.cloud_requests, report.attention) == (1, 1, 1)
    with session_scope(clean_database) as session:
        row = session.scalars(select(LabObservation)).one()
        job = session.scalars(select(LabExtractionJob)).one()
        assert row.status == ReviewStatus.NEEDS_REVIEW
        assert (
            job.status,
            job.safe_error_code,
            job.cloud_attempts,
            job.candidate_count,
        ) == ("needs_attention", "cloud_partial_output", 1, 1)
        assert job.extraction_method == "openai_structured_partial_v3"


def test_cached_import_preserves_spend_and_is_idempotent(clean_database, tmp_path):
    from health_agent.lab_extraction.cached import import_cached

    document, _worker = failed_page(clean_database, tmp_path)
    path = capture(tmp_path, text=TEXT, rows=[GOOD, BAD])
    report = import_cached(clean_database, DEFAULT_PROFILE_ID, document, 1, path)
    assert (report.inserted, report.accepted, report.rejected) == (1, 1, 1)
    assert (
        import_cached(clean_database, DEFAULT_PROFILE_ID, document, 1, path).inserted
        == 0
    )
    with session_scope(clean_database) as session:
        job = session.scalars(select(LabExtractionJob)).one()
        row = session.scalars(select(LabObservation)).one()
        assert (
            job.status,
            job.safe_error_code,
            job.cloud_attempts,
            job.candidate_count,
        ) == ("needs_attention", "cloud_partial_output", 1, 1)
        assert job.model_name == "original-model"
        assert job.extraction_method == "cached_structured_partial_v3"
        assert row.status == ReviewStatus.NEEDS_REVIEW
        assert row.review_item.reason_code == "lab_extraction_v3_cached"
        assert (
            session.get_one(
                LabExtractionProfile, DEFAULT_PROFILE_ID
            ).cloud_requests_today
            == 1
        )
        row.status = ReviewStatus.REJECTED
    assert (
        import_cached(clean_database, DEFAULT_PROFILE_ID, document, 1, path).inserted
        == 0
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "foreign",
        "page",
        "text",
        "digest",
        "completed",
        "unknown",
        "attempt",
        "local",
        "limit",
        "envelope",
        "count",
    ],
)
def test_cached_invalid_is_atomic(clean_database, tmp_path, invalid):
    from uuid import uuid4

    from health_agent.lab_extraction.cached import import_cached

    document, _worker = failed_page(clean_database, tmp_path)
    profile, page = DEFAULT_PROFILE_ID, 1
    rows, text = [GOOD], TEXT
    response = None
    with session_scope(clean_database) as session:
        job = session.scalars(select(LabExtractionJob)).one()
        if invalid == "digest":
            job.source_text_sha256 = "0" * 64
        if invalid == "completed":
            job.status = "completed"
        if invalid == "unknown":
            job.safe_error_code = "cloud_outcome_unknown"
        if invalid == "attempt":
            job.cloud_attempts = 0
        if invalid == "local":
            job.local_completed = False
        if invalid == "limit":
            job.candidate_count = 40
        before = (
            job.status,
            job.safe_error_code,
            job.candidate_count,
            job.cloud_attempts,
            job.extraction_method,
        )
    if invalid == "foreign":
        profile = uuid4()
    if invalid == "page":
        page = 2
    if invalid == "text":
        text += " "
    if invalid == "count":
        rows *= 41
    if invalid == "envelope":
        response = {"choices": []}
    path = capture(tmp_path, text=text, rows=rows, response=response)
    with pytest.raises(ExtractionError):
        import_cached(clean_database, profile, document, page, path)
    with session_scope(clean_database) as session:
        assert session.scalars(select(LabObservation)).all() == []
        job = session.scalars(select(LabExtractionJob)).one()
        assert before == (
            job.status,
            job.safe_error_code,
            job.candidate_count,
            job.cloud_attempts,
            job.extraction_method,
        )
        assert (
            session.get_one(
                LabExtractionProfile, DEFAULT_PROFILE_ID
            ).cloud_requests_today
            == 1
        )
