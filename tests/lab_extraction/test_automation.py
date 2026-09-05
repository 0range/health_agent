from sqlalchemy import select

from health_agent.automation.models import AutomationJob, AutomationResult
from health_agent.automation.registry import LabExtractionJobAdapter
from health_agent.automation.runner import AutomationRunner
from health_agent.automation.storage import AutomationState, GlobalRunLock
from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus
from lab_extraction.test_service import add_page, service


def test_post_sync_worker_observes_new_committed_document_before_projection(
    clean_database, disposable_postgres, tmp_path
):
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    calls = []

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
                add_page(clean_database, "ALT 53 H U/L 0-41")
            elif job.source == "lab_extraction":
                assert worker.run(DEFAULT_PROFILE_ID).inserted == 1
            elif job.source == "sheets":
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
    assert all(result.status == "succeeded" for result in runner.run())
    assert calls == ["drive", "lab_extraction", "sheets"]
