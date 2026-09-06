from uuid import uuid4

from sqlalchemy import select
from typer.testing import CliRunner

import health_agent.cli as main_cli
import health_agent.lab_extraction.cli as extraction_cli
from health_agent.cli import app
from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionJob
from health_agent.lab_extraction.service import LabExtractionService
from health_agent.lab_extraction.types import ExtractionError
from health_agent.lab_extraction.validation import validate_candidates
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus
from lab_extraction.test_service import Cloud, add_page, service


def test_cli_configure_backfill_status_and_disable(
    clean_database, tmp_path, monkeypatch
):
    worker = service(clean_database, tmp_path)
    monkeypatch.setattr(extraction_cli, "build_service", lambda: worker)
    runner = CliRunner()
    profile = str(DEFAULT_PROFILE_ID)
    assert runner.invoke(app, ["lab-extract", "status", profile]).exit_code == 0
    assert runner.invoke(app, ["lab-extract", "configure", profile]).exit_code == 0
    add_page(clean_database)
    result = runner.invoke(app, ["lab-extract", "run", profile])
    assert result.exit_code == 0
    assert "status=succeeded" in result.output and "inserted=1" in result.output
    assert "Glucose" not in result.output
    result = runner.invoke(app, ["lab-extract", "status", profile])
    assert "completed=1" in result.output and "cloud_enabled=false" in result.output
    assert (
        runner.invoke(
            app, ["lab-extract", "configure", profile, "--disabled"]
        ).exit_code
        == 0
    )
    assert (
        "status=deferred" in runner.invoke(app, ["lab-extract", "run", profile]).output
    )


def test_cli_invalid_bounds_and_native_exception_are_content_free(monkeypatch):
    runner = CliRunner()
    profile = str(DEFAULT_PROFILE_ID)
    assert (
        runner.invoke(app, ["lab-extract", "run", profile, "--limit", "21"]).exit_code
        != 0
    )

    def fail():
        raise RuntimeError("SECRET patient lab value API key")

    monkeypatch.setattr(extraction_cli, "build_service", fail)
    result = runner.invoke(app, ["lab-extract", "status", profile])
    assert result.exit_code == 1
    assert "safe_error=extraction_failed" in result.output
    assert "SECRET" not in result.output


def test_cli_uses_neutral_cloud_optin_and_rejects_openai_name_for_yandex(
    clean_database, tmp_path, monkeypatch
):
    configured = Settings(
        _env_file=None,
        ai_provider="yandex",
        yandex_folder_id="synthetic-folder",
        yandex_allowed_profile_ids=(DEFAULT_PROFILE_ID,),
        vault_root=tmp_path / "vault",
        temporary_root=tmp_path / "tmp",
    )
    worker = LabExtractionService(
        clean_database, configured, cloud_extractor=Cloud()
    )
    monkeypatch.setattr(extraction_cli, "build_service", lambda: worker)
    runner = CliRunner()
    profile = str(DEFAULT_PROFILE_ID)
    rejected = runner.invoke(
        app, ["lab-extract", "configure", profile, "--openai"]
    )
    assert rejected.exit_code == 1
    assert "cloud_provider_consent_required" in rejected.output
    accepted = runner.invoke(app, ["lab-extract", "configure", profile, "--cloud"])
    assert accepted.exit_code == 0
    assert "cloud_enabled=true" in accepted.output


def test_cli_unknown_retry_requires_explicit_ack(clean_database, tmp_path, monkeypatch):
    worker = service(
        clean_database,
        tmp_path,
        cloud=Cloud(error=ExtractionError("cloud_outcome_unknown")),
    )
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    document_id = add_page(clean_database, "Glucose\n5.1 mmol/L")
    worker.run(DEFAULT_PROFILE_ID)
    monkeypatch.setattr(extraction_cli, "build_service", lambda: worker)
    command = ["lab-extract", "retry", str(DEFAULT_PROFILE_ID), str(document_id)]
    runner = CliRunner()
    details = runner.invoke(
        app, ["lab-extract", "status", str(DEFAULT_PROFILE_ID), "--details"]
    )
    assert details.exit_code == 0
    assert (
        str(document_id) in details.output
        and "safe_error=cloud_outcome_unknown" in details.output
    )
    assert "Glucose" not in details.output
    blocked = runner.invoke(app, command)
    assert (
        blocked.exit_code == 1
        and "unknown_retry_requires_acknowledgment" in blocked.output
    )
    accepted = runner.invoke(app, [*command, "--acknowledge-unknown"])
    assert accepted.exit_code == 0 and "requeued=1" in accepted.output
    assert worker.run(DEFAULT_PROFILE_ID).cloud_requests == 1
    assert worker.status(DEFAULT_PROFILE_ID).cloud_requests_today == 2


def test_unmapped_candidate_can_be_explicitly_mapped_through_cli(
    clean_database, tmp_path, monkeypatch
):
    text = "Unknown assay 5.1 mmol/L"
    add_page(clean_database, text)

    class UnmappedCloud:
        def extract(self, profile_id, source_text):
            return validate_candidates(
                {
                    "candidates": [
                        {
                            "source_name": "Unknown assay",
                            "source_value": "5.1",
                            "source_unit": "mmol/L",
                            "reference_text": None,
                            "source_flag": None,
                            "evidence_excerpt": text,
                        }
                    ]
                },
                source_text,
            )

    worker = service(clean_database, tmp_path, cloud=UnmappedCloud())
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    worker.run(DEFAULT_PROFILE_ID)
    with session_scope(clean_database) as session:
        original_id = session.scalars(select(LabObservation.id)).one()
    monkeypatch.setattr(main_cli, "build_engine", lambda settings: clean_database)
    command = [
        "review",
        "correct",
        str(original_id),
        "--value",
        "5.1",
        "--unit",
        "mmol/L",
        "--canonical-name",
        "glucose",
        "--profile-id",
    ]
    runner = CliRunner()
    assert runner.invoke(app, [*command, str(uuid4())]).exit_code == 1
    unsupported = command.copy()
    unsupported[8] = "unsupported_analyte"
    assert runner.invoke(app, [*unsupported, str(DEFAULT_PROFILE_ID)]).exit_code == 1
    result = runner.invoke(app, [*command, str(DEFAULT_PROFILE_ID)])
    assert result.exit_code == 0, result.output
    with session_scope(clean_database) as session:
        rows = session.scalars(select(LabObservation)).all()
        assert len(rows) == 2
        verified = next(row for row in rows if row.status is ReviewStatus.VERIFIED)
        assert (
            verified.canonical_name == "glucose"
            and verified.normalized_unit == "mmol/L"
        )
        assert verified.supersedes_observation_id == original_id


def test_details_are_bounded_scoped_and_redact_unknown_codes(
    clean_database, tmp_path, monkeypatch
):
    worker = service(
        clean_database,
        tmp_path,
        cloud=Cloud(error=ExtractionError("cloud_outcome_unknown")),
    )
    worker.configure(DEFAULT_PROFILE_ID, openai=True)
    for _ in range(3):
        add_page(clean_database, "Glucose\n5.1 mmol/L")
    worker.run(DEFAULT_PROFILE_ID, cloud_limit=3)
    with session_scope(clean_database) as session:
        for job in session.scalars(select(LabExtractionJob)):
            job.safe_error_code = "SECRET private health value"
    monkeypatch.setattr(extraction_cli, "build_service", lambda: worker)
    runner = CliRunner()
    command = [
        "lab-extract",
        "status",
        str(DEFAULT_PROFILE_ID),
        "--details",
        "--limit",
        "1",
    ]
    first = runner.invoke(app, command)
    second = runner.invoke(app, [*command, "--offset", "1"])
    assert first.exit_code == second.exit_code == 0
    assert (
        first.output.count("document_id=") == second.output.count("document_id=") == 1
    )
    assert (
        first.output != second.output and "safe_error=extraction_failed" in first.output
    )
    assert "SECRET" not in first.output
    other = runner.invoke(app, ["lab-extract", "status", str(uuid4()), "--details"])
    assert "document_id=" not in other.output
