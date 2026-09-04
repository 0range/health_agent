from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import UUID

import pymupdf
import pytest
from conftest import DisposablePostgres, require_disposable_database
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from health_agent import cli

_SYNTHETIC_EVIDENCE = "Ferritin 42 ng/mL 30-400"


def _output_uuid(output: str, field: str) -> UUID:
    match = re.search(rf"(?:^| ){re.escape(field)}=([0-9a-f-]{{36}})(?: |$)", output)
    assert match is not None
    return UUID(match.group(1))


@pytest.mark.parametrize(
    "database_name",
    ("health_agent", "test_health_agent_e2e", "test_health_agent_e2e_not-a-uuid"),
)
def test_disposable_database_guard_rejects_unsafe_names(database_name: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing destructive operation"):
        require_disposable_database(database_name)


def test_synthetic_pdf_reaches_verified_history_once(
    clean_database: Engine,
    disposable_postgres: DisposablePostgres,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = clean_database
    settings = disposable_postgres.settings.model_copy(
        update={"vault_root": tmp_path / "vault"}
    )
    pdf_path = tmp_path / "synthetic-lab.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), _SYNTHETIC_EVIDENCE)
        pdf.save(pdf_path)

    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    source_uri = "synthetic:e2e"
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    runner = CliRunner()

    first_import = runner.invoke(
        cli.app,
        ["import-file", str(pdf_path), "--source-uri", source_uri],
    )
    second_import = runner.invoke(
        cli.app,
        ["import-file", str(pdf_path), "--source-uri", source_uri],
    )

    assert first_import.exit_code == 0
    assert "status=imported" in first_import.stdout
    assert "candidates=1 review_items=1" in first_import.stdout
    assert second_import.exit_code == 0
    assert "status=duplicate" in second_import.stdout
    first_document_id = _output_uuid(first_import.stdout, "document_id")
    duplicate_document_id = _output_uuid(second_import.stdout, "document_id")
    assert duplicate_document_id == first_document_id

    with engine.connect() as connection:
        source = connection.execute(
            text(
                "SELECT id, provider, external_id, revision, source_uri "
                "FROM source_records"
            )
        ).one()
        document = connection.execute(
            text(
                "SELECT id, source_record_id, sha256, vault_path "
                "FROM documents"
            )
        ).one()
        document_page = connection.execute(
            text(
                "SELECT id, document_id, page_number, extraction_method "
                "FROM document_pages"
            )
        ).one()
        observation = connection.execute(
            text(
                "SELECT id, document_id, page_number, canonical_name, "
                "evidence_excerpt, status::text AS status FROM lab_observations"
            )
        ).one()

    assert (source.provider, source.external_id, source.revision, source.source_uri) == (
        "local_file",
        pdf_path.name,
        f"sha256:{digest}",
        source_uri,
    )
    assert document.id == first_document_id
    assert document.source_record_id == source.id
    assert document.sha256 == digest
    assert Path(document.vault_path).resolve() == (
        settings.vault_root / digest[:2] / digest
    ).resolve()
    assert document_page.document_id == document.id
    assert document_page.page_number == 1
    assert document_page.extraction_method == "digital_text"
    assert observation.document_id == document.id
    assert observation.page_number == document_page.page_number
    assert observation.canonical_name == "ferritin"
    assert observation.evidence_excerpt == _SYNTHETIC_EVIDENCE
    assert observation.status == "needs_review"

    review = runner.invoke(cli.app, ["review", "approve", str(observation.id)])

    assert review.exit_code == 0
    assert f"status=approved observation_id={observation.id}" in review.stdout

    with engine.connect() as connection:
        counts = {
            table_name: connection.execute(
                text(f"SELECT count(*) FROM {table_name}")
            ).scalar_one()
            for table_name in (
                "source_records",
                "documents",
                "document_pages",
                "lab_observations",
                "review_items",
                "verified_lab_history",
            )
        }
        decision = connection.execute(
            text(
                "SELECT observation_id, decision, resolved_at FROM review_items"
            )
        ).one()
        verified = connection.execute(
            text(
                "SELECT id, document_id, page_number, canonical_name, "
                "evidence_excerpt, status::text AS status "
                "FROM verified_lab_history"
            )
        ).one()

    vault_objects = [path for path in settings.vault_root.rglob("*") if path.is_file()]

    assert vault_objects == [settings.vault_root / digest[:2] / digest]
    assert counts == {
        "source_records": 1,
        "documents": 1,
        "document_pages": 1,
        "lab_observations": 1,
        "review_items": 1,
        "verified_lab_history": 1,
    }
    assert decision.observation_id == observation.id
    assert decision.decision == "approved"
    assert decision.resolved_at is not None
    assert verified.id == observation.id
    assert verified.document_id == document.id
    assert verified.page_number == document_page.page_number
    assert verified.canonical_name == "ferritin"
    assert verified.evidence_excerpt == _SYNTHETIC_EVIDENCE
    assert verified.status == "verified"
