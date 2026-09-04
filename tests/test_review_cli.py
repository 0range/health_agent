from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine
from typer.testing import CliRunner

from health_agent import cli
from health_agent.db import session_scope
from health_agent.importer import ImportReport
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    DocumentSourceRecord,
    LabObservation,
    ReviewStatus,
    SourceRecord,
)


def test_import_output_contains_only_safe_counts(monkeypatch, tmp_path: Path) -> None:
    class FakeSettings:
        vault_root = tmp_path / "vault"

    @contextmanager
    def fake_session_scope(_engine: object):
        yield object()

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli, "FileVault", lambda _root: object())
    monkeypatch.setattr(
        cli,
        "import_document",
        lambda _session, _vault, _path, _source_uri, **_kwargs: ImportReport(
            status="imported",
            processing_status="needs_review",
            document_id=UUID("00000000-0000-0000-0000-000000000001"),
            candidate_count=1,
            review_count=1,
        ),
    )

    result = CliRunner().invoke(cli.app, ["import-file", str(tmp_path / "labs.pdf")])

    assert result.exit_code == 0
    assert result.stdout == (
        "status=imported document_id=00000000-0000-0000-0000-000000000001 "
        "processing_status=needs_review candidates=1 review_items=1\n"
    )


def test_review_commands_are_registered() -> None:
    result = CliRunner().invoke(cli.app, ["review", "--help"])

    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "approve" in result.stdout
    assert "reject" in result.stdout


def test_review_list_survives_real_session_commit_and_close(
    clean_database: Engine,
    monkeypatch,
) -> None:
    document_id = uuid4()
    source_record_id = uuid4()
    observation_id = uuid4()
    with session_scope(clean_database) as session:
        session.add_all(
            [
                SourceRecord(
                    id=source_record_id,
                    profile_id=DEFAULT_PROFILE_ID,
                    provider="local_file",
                    external_id="results.pdf",
                    revision="sha256:test",
                    source_uri=None,
                ),
                Document(
                    id=document_id,
                    profile_id=DEFAULT_PROFILE_ID,
                    sha256="a" * 64,
                    vault_path="vault/results.pdf",
                    media_type="application/pdf",
                    document_type="lab_report",
                    issued_date=date(2026, 8, 30),
                    collected_date=date(2026, 8, 29),
                    processing_status="needs_review",
                    safe_error_code=None,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                DocumentSourceRecord(
                    document_id=document_id,
                    source_record_id=source_record_id,
                    profile_id=DEFAULT_PROFILE_ID,
                ),
                DocumentPage(
                    document_id=document_id,
                    page_number=1,
                    extracted_text="Ferritin 42 ng/mL",
                    extraction_method="text",
                ),
            ]
        )
        session.flush()
        session.add(
            LabObservation(
                id=observation_id,
                document_id=document_id,
                supersedes_observation_id=None,
                page_number=1,
                canonical_name="ferritin",
                source_name="Ferritin",
                source_value="42",
                parsed_value=Decimal(42),
                source_unit="ng/mL",
                normalized_value=None,
                normalized_unit=None,
                reference_low=None,
                reference_high=None,
                reference_text=None,
                evidence_excerpt="Ferritin 42 ng/mL",
                confidence=0.8,
                status=ReviewStatus.NEEDS_REVIEW,
            )
        )
    monkeypatch.setenv(
        "DATABASE_URL", clean_database.url.render_as_string(hide_password=False)
    )

    result = CliRunner().invoke(cli.app, ["review", "list"])

    assert result.exit_code == 0
    assert f"observation_id={observation_id}" in result.stdout
    assert "source_name=Ferritin source_value=42 source_unit=ng/mL" in result.stdout
    assert "filename=results.pdf" in result.stdout


def test_import_output_surfaces_ocr_required(monkeypatch, tmp_path: Path) -> None:
    class FakeSettings:
        vault_root = tmp_path / "vault"

    @contextmanager
    def fake_session_scope(_engine: object):
        yield object()

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli, "FileVault", lambda _root: object())
    monkeypatch.setattr(
        cli,
        "import_document",
        lambda _session, _vault, _path, _source_uri, **_kwargs: ImportReport(
            status="ocr_required",
            processing_status="ocr_required",
            document_id=UUID("00000000-0000-0000-0000-000000000002"),
            candidate_count=0,
            review_count=0,
        ),
    )

    result = CliRunner().invoke(cli.app, ["import-file", str(tmp_path / "scan.pdf")])

    assert result.exit_code == 0
    assert "status=ocr_required" in result.stdout
    assert "processing_status=ocr_required" in result.stdout
    assert "candidates=0 review_items=0" in result.stdout
