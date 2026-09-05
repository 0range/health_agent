from typer.testing import CliRunner

import health_agent.lab_extraction.cli as extraction_cli
from health_agent.cli import app
from health_agent.lab_extraction.types import ExtractionError
from health_agent.models import DEFAULT_PROFILE_ID
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
    blocked = runner.invoke(app, command)
    assert (
        blocked.exit_code == 1
        and "unknown_retry_requires_acknowledgment" in blocked.output
    )
    accepted = runner.invoke(app, [*command, "--acknowledge-unknown"])
    assert accepted.exit_code == 0 and "requeued=1" in accepted.output
