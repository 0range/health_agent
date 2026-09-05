import pytest
from sqlalchemy import select
from typer.testing import CliRunner

import health_agent.lab_extraction.cli as extraction_cli
from health_agent.automation.models import AutomationJob, AutomationResult
from health_agent.automation.registry import LabExtractionJobAdapter
from health_agent.automation.runner import AutomationRunner
from health_agent.automation.storage import AutomationState, GlobalRunLock
from health_agent.cli import app
from health_agent.db import session_scope
from health_agent.lab_extraction.types import ExtractionError
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus
from lab_extraction.test_service import Cloud, add_page, service


@pytest.mark.parametrize("unknown", [False, True])
def test_post_sync_worker_observes_new_committed_document_before_projection(
    clean_database, disposable_postgres, tmp_path, monkeypatch, unknown
):
    worker = service(
        clean_database,
        tmp_path,
        cloud=Cloud(error=ExtractionError("cloud_outcome_unknown")),
    )
    worker.configure(DEFAULT_PROFILE_ID, openai=unknown)
    calls = []
    documents = []

    class Connectors:
        source = "drive"

        def discover(self, settings):
            return tuple(
                AutomationJob(
                    source, str(DEFAULT_PROFILE_ID), "main", False, (source, "sync")
                )
                for source in ("sheets", "drive")
            )

    class Executor:
        def execute(self, job, mode):
            calls.append(job.source)
            if job.source == "drive":
                documents.append(
                    add_page(
                        clean_database,
                        "Glucose\n5.1 mmol/L" if unknown else "ALT 53 H U/L 0-41",
                    )
                )
            elif job.source == "lab_extraction":
                report = worker.run(DEFAULT_PROFILE_ID)
                assert report.inserted == (0 if unknown else 1)
                return AutomationResult(
                    *job.key, mode, "deferred" if unknown else "succeeded"
                )
            elif job.source == "sheets" and not unknown:
                with session_scope(clean_database) as session:
                    row = session.scalars(select(LabObservation)).one()
                    assert (
                        row.status is ReviewStatus.NEEDS_REVIEW
                        and row.source_flag == "H"
                    )
            return AutomationResult(*job.key, mode, "succeeded")

    runner = AutomationRunner(
        disposable_postgres.settings,
        [Connectors(), LabExtractionJobAdapter()],
        Executor(),
        AutomationState(tmp_path / "automation" / "state.json"),
        GlobalRunLock(tmp_path / "automation" / "run.lock"),
    )
    assert all(result.status in {"succeeded", "deferred"} for result in runner.run())
    assert calls == ["drive", "lab_extraction", "sheets"]
    if unknown:
        monkeypatch.setattr(extraction_cli, "build_service", lambda: worker)
        cli = CliRunner()
        details = cli.invoke(
            app, ["lab-extract", "status", str(DEFAULT_PROFILE_ID), "--details"]
        )
        assert details.exit_code == 0 and str(documents[0]) in details.output
        assert (
            "safe_error=cloud_outcome_unknown" in details.output
            and "Glucose" not in details.output
        )
        retry = cli.invoke(
            app,
            [
                "lab-extract",
                "retry",
                str(DEFAULT_PROFILE_ID),
                str(documents[0]),
                "--acknowledge-unknown",
            ],
        )
        assert retry.exit_code == 0
        assert worker.status(DEFAULT_PROFILE_ID).cloud_requests_today == 1
