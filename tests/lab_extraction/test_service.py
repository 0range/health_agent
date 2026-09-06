"""Offline queue, restart and review-only integration regressions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.importer import (
    approve_observation,
    correct_observation,
    reject_observation,
)
from health_agent.lab_extraction.models import LabExtractionJob
from health_agent.lab_extraction.queue import profile_lock
from health_agent.lab_extraction.service import LabExtractionService
from health_agent.lab_extraction.types import ExtractionError
from health_agent.lab_extraction.validation import parse_local, validate_candidates
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)


def add_page(
    engine, text="Glucose 5.1 mmol/L 3.9-5.5", *, profile_id=DEFAULT_PROFILE_ID
):
    with session_scope(engine) as session:
        document = Document(
            profile_id=profile_id,
            sha256=uuid4().hex * 2,
            vault_path="unused-synthetic-vault",
            media_type="application/pdf",
            document_type="laboratory_report",
            processing_status="needs_attention",
            safe_error_code="no_lab_candidates",
        )
        session.add(document)
        session.flush()
        session.add(
            DocumentPage(
                document_id=document.id,
                page_number=1,
                extracted_text=text,
                extraction_method="digital_text",
            )
        )
        return document.id


class Cloud:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def extract(self, profile_id, text):
        self.calls.append((profile_id, text))
        if self.error:
            raise self.error
        return validate_candidates(
            {
                "candidates": [
                    {
                        "source_name": "Glucose",
                        "source_value": "5.1",
                        "source_unit": "mmol/L",
                        "reference_text": None,
                        "source_flag": None,
                        "evidence_excerpt": text,
                    }
                ]
            },
            text,
        )


def service(engine, tmp_path, *, cloud=None, clock=None, local_reader=None):
    settings = Settings(
        _env_file=None, vault_root=tmp_path / "vault", temporary_root=tmp_path / "tmp"
    )
    return LabExtractionService(
        engine, settings, cloud_extractor=cloud, clock=clock, local_reader=local_reader
    )


def test_local_backfill_is_review_only_and_idempotent(clean_database, tmp_path):
    document_id = add_page(clean_database, "ALT 53 H U/L 0-41")
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    result = worker.run(DEFAULT_PROFILE_ID)
    assert result.processed == 1 and result.inserted == 1 and result.cloud_requests == 0
    with session_scope(clean_database) as session:
        row = session.scalars(select(LabObservation)).one()
        assert (
            row.document_id == document_id and row.status is ReviewStatus.NEEDS_REVIEW
        )
        assert row.normalized_value is None
        assert row.source_flag == "H"
        assert (row.reference_low, row.reference_high) == (Decimal(0), Decimal(41))
        assert row.evidence_excerpt == "ALT 53 H U/L 0-41"
        assert row.review_item.reason_code == "lab_extraction_v1_local"
        item_id = row.id
        approve_observation(session, item_id)
    assert worker.run(DEFAULT_PROFILE_ID).processed == 0
    with session_scope(clean_database) as session:
        session.scalars(select(LabExtractionJob)).one().status = "queued"
    assert worker.run(DEFAULT_PROFILE_ID).inserted == 0
    with session_scope(clean_database) as session:
        assert len(session.scalars(select(LabObservation)).all()) == 1
        assert session.get_one(LabObservation, item_id).status is ReviewStatus.VERIFIED


def test_profiles_and_batch_limits_are_isolated(clean_database, tmp_path):
    other = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Other synthetic"))
    for _ in range(3):
        add_page(clean_database)
    add_page(clean_database, profile_id=other)
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    assert worker.run(DEFAULT_PROFILE_ID, limit=2).processed == 2
    with session_scope(clean_database) as session:
        assert len(session.scalars(select(LabObservation)).all()) == 2
        assert set(session.scalars(select(LabExtractionJob.profile_id)).all()) == {
            DEFAULT_PROFILE_ID
        }


def test_cloud_requires_profile_optin_and_waits_for_per_run_budget(
    clean_database, tmp_path
):
    for _ in range(3):
        add_page(clean_database, "Glucose\n5.1 mmol/L")
    cloud = Cloud()
    worker = service(clean_database, tmp_path, cloud=cloud)
    worker.configure(DEFAULT_PROFILE_ID)
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 0
    assert not cloud.calls
    assert worker.status(DEFAULT_PROFILE_ID).waiting_cloud == 3
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    result = worker.run(DEFAULT_PROFILE_ID, cloud_limit=1)
    assert result.cloud_requests == 1 and len(cloud.calls) == 1
    assert worker.status(DEFAULT_PROFILE_ID).waiting_cloud == 2


def test_yandex_consent_denial_preserves_local_and_reserves_no_cloud_call(
    clean_database, tmp_path
):
    add_page(clean_database, "Glucose\n5.1 mmol/L")
    settings = Settings(
        _env_file=None,
        ai_provider="yandex",
        yandex_folder_id="synthetic-folder",
        vault_root=tmp_path / "vault",
        temporary_root=tmp_path / "tmp",
    )
    worker = LabExtractionService(clean_database, settings)
    worker.configure(DEFAULT_PROFILE_ID, cloud=True)
    result = worker.run(DEFAULT_PROFILE_ID)
    assert result.cloud_requests == 0
    assert worker.status(DEFAULT_PROFILE_ID).cloud_requests_today == 0
    with session_scope(clean_database) as session:
        job = session.scalars(select(LabExtractionJob)).one()
        assert job.safe_error_code == "cloud_provider_consent_required"
        assert job.local_completed is True


def test_yandex_records_actual_model_and_extraction_method(clean_database, tmp_path):
    add_page(clean_database, "Glucose\n5.1 mmol/L")
    cloud = Cloud()
    settings = Settings(
        _env_file=None,
        ai_provider="yandex",
        yandex_folder_id="synthetic-folder",
        yandex_allowed_profile_ids=(DEFAULT_PROFILE_ID,),
        vault_root=tmp_path / "vault",
        temporary_root=tmp_path / "tmp",
    )
    worker = LabExtractionService(clean_database, settings, cloud_extractor=cloud)
    worker.configure(DEFAULT_PROFILE_ID, cloud=True)
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 1
    with session_scope(clean_database) as session:
        job = session.scalars(select(LabExtractionJob)).one()
        assert job.model_name == "gpt://synthetic-folder/qwen3.6-35b-a3b"
        assert job.extraction_method == "yandex_structured"


def test_unknown_cloud_outcome_is_not_retried_on_restart(clean_database, tmp_path):
    document_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    cloud = Cloud(error=ExtractionError("cloud_outcome_unknown"))
    worker = service(clean_database, tmp_path, cloud=cloud)
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    assert worker.run(DEFAULT_PROFILE_ID).attention == 1
    restarted = service(clean_database, tmp_path, cloud=cloud)
    assert restarted.run(DEFAULT_PROFILE_ID).cloud_requests == 0
    assert len(cloud.calls) == 1
    with pytest.raises(ExtractionError, match="unknown_retry_requires_acknowledgment"):
        restarted.retry(DEFAULT_PROFILE_ID, document_id)
    restarted.retry(DEFAULT_PROFILE_ID, document_id, acknowledge_unknown=True)
    restarted.run(DEFAULT_PROFILE_ID)
    assert len(cloud.calls) == 2


@pytest.mark.parametrize(
    "safe_code",
    ["cloud_quota_exhausted", "cloud_auth_required", "cloud_rate_limited"],
)
def test_bounded_cloud_failure_stops_cloud_calls_but_keeps_local_processing(
    clean_database, tmp_path, safe_code
):
    for _ in range(3):
        add_page(clean_database, "")
    local_calls = []

    def local_reader(snapshot, page_number, vault_root, temporary_root):
        local_calls.append(snapshot.id)
        return f"Unknown marker {len(local_calls)}"

    cloud = Cloud(error=ExtractionError(safe_code))
    worker = service(
        clean_database, tmp_path, cloud=cloud, local_reader=local_reader
    )
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    result = worker.run(DEFAULT_PROFILE_ID, limit=3, cloud_limit=3)
    assert result.processed == 3
    assert result.cloud_requests == 1
    assert len(cloud.calls) == 1
    assert len(local_calls) == 3
    assert worker.status(DEFAULT_PROFILE_ID).waiting_cloud == 2


def test_cloud_crash_fence_and_daily_budget_survive_restart(clean_database, tmp_path):
    first_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    add_page(clean_database, "Glucose\n5.1 mmol/L")
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    cloud = Cloud()
    worker = service(clean_database, tmp_path, cloud=cloud, clock=lambda: now)
    worker.configure(DEFAULT_PROFILE_ID, openai=True, daily_budget=1)
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 1
    assert (
        service(clean_database, tmp_path, cloud=cloud, clock=lambda: now)
        .run(DEFAULT_PROFILE_ID)
        .cloud_requests
        == 0
    )
    assert (
        service(
            clean_database, tmp_path, cloud=cloud, clock=lambda: now + timedelta(days=1)
        )
        .run(DEFAULT_PROFILE_ID)
        .cloud_requests
        == 1
    )
    with session_scope(clean_database) as session:
        job = session.scalars(
            select(LabExtractionJob).where(LabExtractionJob.document_id == first_id)
        ).one()
        job.status = "cloud_in_flight"
        job.claim_token = uuid4()
    restarted = service(
        clean_database, tmp_path, cloud=cloud, clock=lambda: now + timedelta(days=1)
    )
    assert restarted.run(DEFAULT_PROFILE_ID).cloud_requests == 0
    with session_scope(clean_database) as session:
        job = session.scalars(
            select(LabExtractionJob).where(LabExtractionJob.document_id == first_id)
        ).one()
        assert job.safe_error_code == "cloud_outcome_unknown"


def test_ocr_only_fills_empty_page_and_rejected_rows_never_resurrect(
    clean_database, tmp_path
):
    document_id = add_page(clean_database, "")
    calls = []

    def local(snapshot, page_number, vault_root: Path, temporary_root: Path):
        calls.append(snapshot.id)
        return "Glucose 5.1 mmol/L"

    worker = service(clean_database, tmp_path, local_reader=local)
    worker.configure(DEFAULT_PROFILE_ID)
    worker.run(DEFAULT_PROFILE_ID)
    with session_scope(clean_database) as session:
        row = session.scalars(select(LabObservation)).one()
        reject_observation(session, row.id)
        job = session.scalars(select(LabExtractionJob)).one()
        job.status = "queued"
        job.local_completed = False
    assert worker.run(DEFAULT_PROFILE_ID).inserted == 0
    assert calls == [document_id]


def test_recovered_page_method_does_not_overclaim_ocr(clean_database, tmp_path):
    add_page(clean_database, "")
    worker = service(
        clean_database, tmp_path, local_reader=lambda *args: "Glucose 5.1 mmol/L"
    )
    worker.configure(DEFAULT_PROFILE_ID)
    worker.run(DEFAULT_PROFILE_ID)
    with session_scope(clean_database) as session:
        assert (
            session.scalars(select(DocumentPage)).one().extraction_method
            == "local_text_or_ocr"
        )


def test_concurrent_worker_and_configuration_are_fenced(clean_database, tmp_path):
    add_page(clean_database)
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    with profile_lock(clean_database, DEFAULT_PROFILE_ID):
        with pytest.raises(ExtractionError, match="extraction_busy"):
            worker.run(DEFAULT_PROFILE_ID)
        with pytest.raises(ExtractionError, match="extraction_busy"):
            worker.configure(DEFAULT_PROFILE_ID, openai=True)
    assert worker.run(DEFAULT_PROFILE_ID).inserted == 1


def test_recovered_local_claim_cannot_publish_late(clean_database, tmp_path):
    add_page(clean_database)
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    worker.queue.discover_and_recover(DEFAULT_PROFILE_ID)
    job_id = worker.queue.pending(DEFAULT_PROFILE_ID, 1, cloud=False)[0]
    stale = worker.queue.claim(DEFAULT_PROFILE_ID, job_id)
    assert worker.run(DEFAULT_PROFILE_ID).inserted == 1
    with pytest.raises(ExtractionError, match="stale_extraction_claim"):
        worker.queue.publish(stale, stale.text, (), cloud=False)


def test_cloud_lifetime_cap_cannot_be_reset_by_retry(clean_database, tmp_path):
    document_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    cloud = Cloud(error=ExtractionError("cloud_outcome_unknown"))
    worker = service(clean_database, tmp_path, cloud=cloud)
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    for _ in range(3):
        worker.run(DEFAULT_PROFILE_ID)
        worker.retry(DEFAULT_PROFILE_ID, document_id, acknowledge_unknown=True)
    assert len(cloud.calls) == 3
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 0
    assert worker.retry(DEFAULT_PROFILE_ID, document_id, acknowledge_unknown=True) == 0


def test_page_text_change_and_foreign_retry_fail_closed(clean_database, tmp_path):
    document_id = add_page(clean_database)
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    worker.queue.discover_and_recover(DEFAULT_PROFILE_ID)
    claim = worker.queue.claim(
        DEFAULT_PROFILE_ID, worker.queue.pending(DEFAULT_PROFILE_ID, 1, cloud=False)[0]
    )
    with session_scope(clean_database) as session:
        session.scalars(select(DocumentPage)).one().extracted_text = "Different source"
    with pytest.raises(ExtractionError, match="page_evidence_changed"):
        worker.queue.publish(claim, claim.text, (), cloud=False)
    with pytest.raises(ExtractionError, match="document_not_found"):
        worker.retry(uuid4(), document_id, acknowledge_unknown=True)


def test_document_date_conflict_is_preserved(clean_database, tmp_path):
    document_id = add_page(clean_database)
    with session_scope(clean_database) as session:
        document = session.get_one(Document, document_id)
        document.safe_error_code = "conflicting_medical_date"
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    worker.run(DEFAULT_PROFILE_ID)
    with session_scope(clean_database) as session:
        document = session.get_one(Document, document_id)
        assert document.processing_status == "needs_attention"
        assert document.safe_error_code == "conflicting_medical_date"


def test_local_cloud_aggregate_candidate_cap_is_atomic(clean_database, tmp_path):
    text = "\n".join(f"Marker{i} {i} U/L" for i in range(41))
    add_page(clean_database, text)
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    worker.queue.discover_and_recover(DEFAULT_PROFILE_ID)
    claim = worker.queue.claim(
        DEFAULT_PROFILE_ID, worker.queue.pending(DEFAULT_PROFILE_ID, 1, cloud=True)[0]
    )
    candidates = tuple(parse_local(line).candidates[0] for line in text.splitlines())
    worker.queue.publish(claim, text, candidates[:40], cloud=False, unresolved=True)
    worker.queue.reserve_cloud(
        claim, datetime.now(UTC).date(), "synthetic-model", allowed=True
    )
    with pytest.raises(ExtractionError, match="candidate_limit"):
        worker.queue.publish(claim, text, candidates[40:], cloud=True)
    with session_scope(clean_database) as session:
        assert len(session.scalars(select(LabObservation)).all()) == 40


def test_version_bump_preserves_unknown_acknowledgment_fence(clean_database, tmp_path):
    document_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    cloud = Cloud(error=ExtractionError("cloud_outcome_unknown"))
    worker = service(clean_database, tmp_path, cloud=cloud)
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    worker.run(DEFAULT_PROFILE_ID)
    with session_scope(clean_database) as session:
        session.scalars(
            select(LabExtractionJob)
        ).one().extractor_version = "older-version"
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 0
    assert len(cloud.calls) == 1
    with pytest.raises(ExtractionError, match="unknown_retry_requires_acknowledgment"):
        worker.retry(DEFAULT_PROFILE_ID, document_id)
    worker.retry(DEFAULT_PROFILE_ID, document_id, acknowledge_unknown=True)
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 1
    assert len(cloud.calls) == 2


def test_version_bump_cannot_reset_page_lifetime_cost(clean_database, tmp_path):
    document_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    cloud = Cloud(error=ExtractionError("cloud_outcome_unknown"))
    worker = service(clean_database, tmp_path, cloud=cloud)
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    for _ in range(3):
        worker.run(DEFAULT_PROFILE_ID)
        worker.retry(DEFAULT_PROFILE_ID, document_id, acknowledge_unknown=True)
    with session_scope(clean_database) as session:
        session.scalars(
            select(LabExtractionJob)
        ).one().extractor_version = "older-version"
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 0
    assert len(cloud.calls) == 3


def test_corrected_source_replay_retains_flag_and_correction(clean_database, tmp_path):
    add_page(clean_database, "ALT 53 H U/L 0-41")
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    worker.run(DEFAULT_PROFILE_ID)
    with session_scope(clean_database) as session:
        original = session.scalars(select(LabObservation)).one()
        corrected = correct_observation(
            session, original.id, source_value="52", source_unit="U/L"
        )
        assert corrected.source_flag == "H"
        session.scalars(select(LabExtractionJob)).one().status = "queued"
    assert worker.run(DEFAULT_PROFILE_ID).inserted == 0
    with session_scope(clean_database) as session:
        rows = session.scalars(select(LabObservation)).all()
        assert len(rows) == 2
        assert [
            row.source_value for row in rows if row.status is ReviewStatus.VERIFIED
        ] == ["52"]


def test_stale_cloud_claim_cannot_publish_after_ack_and_reclaim(
    clean_database, tmp_path
):
    document_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    worker.queue.discover_and_recover(DEFAULT_PROFILE_ID)
    job_id = worker.queue.pending(DEFAULT_PROFILE_ID, 1, cloud=True)[0]
    stale = worker.queue.claim(DEFAULT_PROFILE_ID, job_id)
    worker.queue.publish(stale, stale.text, (), cloud=False, unresolved=True)
    worker.queue.reserve_cloud(
        stale, datetime.now(UTC).date(), "synthetic", allowed=True
    )
    worker.queue.discover_and_recover(DEFAULT_PROFILE_ID)
    worker.retry(DEFAULT_PROFILE_ID, document_id, acknowledge_unknown=True)
    current = worker.queue.claim(DEFAULT_PROFILE_ID, job_id)
    assert current.token != stale.token
    with pytest.raises(ExtractionError, match="stale_extraction_claim"):
        worker.queue.publish(stale, stale.text, (), cloud=True)
