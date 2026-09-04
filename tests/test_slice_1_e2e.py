from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from alembic import command
from health_agent import cli
from health_agent.config import Settings
from health_agent.db import build_engine


@pytest.fixture(scope="module")
def engine() -> Engine:
    settings = Settings()
    assert settings.database_url is not None
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_config, "head")
    return build_engine(settings)


def test_synthetic_pdf_reaches_verified_history_once(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with engine.begin() as connection:
        for table in (
            "review_items",
            "lab_observations",
            "document_pages",
            "documents",
            "source_records",
        ):
            connection.execute(text(f"DELETE FROM {table}"))

    pdf_path = tmp_path / "synthetic-lab.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Ferritin 42 ng/mL 30-400")
    pdf.save(pdf_path)
    pdf.close()

    settings = Settings(vault_root=tmp_path / "vault")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    runner = CliRunner()

    first_import = runner.invoke(
        cli.app,
        ["import-file", str(pdf_path), "--source-uri", "synthetic:e2e"],
    )
    second_import = runner.invoke(
        cli.app,
        ["import-file", str(pdf_path), "--source-uri", "synthetic:e2e"],
    )

    assert first_import.exit_code == 0
    assert "status=imported" in first_import.stdout
    assert "candidates=1 review_items=1" in first_import.stdout
    assert second_import.exit_code == 0
    assert "status=duplicate" in second_import.stdout

    with engine.connect() as connection:
        observation_id = connection.execute(
            text("SELECT id FROM lab_observations")
        ).scalar_one()

    review = runner.invoke(cli.app, ["review", "approve", str(observation_id)])

    assert review.exit_code == 0
    assert f"status=approved observation_id={observation_id}" in review.stdout

    with engine.connect() as connection:
        counts = {
            "documents": connection.execute(
                text("SELECT count(*) FROM documents")
            ).scalar_one(),
            "observations": connection.execute(
                text("SELECT count(*) FROM lab_observations")
            ).scalar_one(),
            "decisions": connection.execute(
                text(
                    "SELECT count(*) FROM review_items "
                    "WHERE decision = 'approved' AND resolved_at IS NOT NULL"
                )
            ).scalar_one(),
            "verified_history": connection.execute(
                text("SELECT count(*) FROM verified_lab_history")
            ).scalar_one(),
        }

    vault_objects = [path for path in settings.vault_root.rglob("*") if path.is_file()]

    assert len(vault_objects) == 1
    assert counts == {
        "documents": 1,
        "observations": 1,
        "decisions": 1,
        "verified_history": 1,
    }
