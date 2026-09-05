from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from health_agent.db import session_scope
from health_agent.google_sheets.config import WORKBOOK_SCHEMA_VERSION
from health_agent.google_sheets.models import SheetsSyncRun
from health_agent.google_sheets.service import (
    SheetsService,
    SheetsSyncFailure,
    WorkbookOwnershipError,
)
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsStateStore,
)
from health_agent.google_sheets.types import (
    CreatedWorkbook,
    SheetsAccountIdentity,
    SheetValue,
    WorkbookBinding,
    WorkbookProjection,
)
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus

from .helpers import add_observation


class FakeOAuth:
    def authorize(
        self, profile_id: str, *, force: bool = False, interactive: bool = False
    ) -> None:
        del profile_id, force, interactive

    def load(self, profile_id: str):
        del profile_id
        return object()

    def local_status(self, profile_id: str) -> str:
        del profile_id
        return "ready"


class FakeGateway:
    def __init__(self) -> None:
        self.binding: WorkbookBinding | None = None
        self.created = 0
        self.writes = 0
        self.review_reads = 0
        self.review_rows: tuple[tuple[SheetValue, ...], ...] = ()
        self.latest: WorkbookProjection | None = None
        self.fail_next_write = False

    def account_identity(self) -> SheetsAccountIdentity:
        return SheetsAccountIdentity("permission-1", "me@example.com")

    def create_workbook(self, title: str, binding: WorkbookBinding) -> CreatedWorkbook:
        del title
        self.created += 1
        self.binding = binding
        return CreatedWorkbook(
            "spreadsheet_123",
            "https://docs.google.com/spreadsheets/d/spreadsheet_123/edit",
        )

    def read_binding(self, spreadsheet_id: str) -> WorkbookBinding:
        del spreadsheet_id
        assert self.binding is not None
        return self.binding

    def read_review_rows(self, spreadsheet_id: str):
        del spreadsheet_id
        self.review_reads += 1
        return self.review_rows

    def replace_managed_tabs(
        self, spreadsheet_id: str, projection: WorkbookProjection
    ) -> None:
        del spreadsheet_id
        self.writes += 1
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("private remote payload")
        self.latest = projection
        review = next(
            sheet for sheet in projection.sheets if sheet.title == "Needs review"
        )
        self.review_rows = (review.headers, *review.rows)


def _service(tmp_path: Path, engine, gateway: FakeGateway) -> SheetsService:
    profiles = LocalSheetsProfileStore(tmp_path / "sheets")
    state = LocalSheetsStateStore(tmp_path / "sheets")
    oauth = FakeOAuth()
    return SheetsService(
        profiles,
        state,
        oauth,  # type: ignore[arg-type]
        lambda credentials: gateway,
        lambda: session_scope(engine),
        lambda session, profile_id: (),
    )


def test_first_and_repeat_sync_create_exactly_one_workbook(
    tmp_path: Path, clean_database
) -> None:
    gateway = FakeGateway()
    service = _service(tmp_path, clean_database, gateway)
    service.configure(
        DEFAULT_PROFILE_ID,
        expected_permission_id="permission-1",
        expected_email="me@example.com",
    )
    first = service.sync(DEFAULT_PROFILE_ID)
    second = service.sync(DEFAULT_PROFILE_ID)
    assert first.status == second.status == "succeeded"
    assert gateway.created == 1
    assert gateway.writes == 2
    assert service.status(DEFAULT_PROFILE_ID).spreadsheet_configured is True


def test_wrong_workbook_binding_aborts_before_review_or_write(
    tmp_path: Path, clean_database
) -> None:
    gateway = FakeGateway()
    service = _service(tmp_path, clean_database, gateway)
    service.configure(
        DEFAULT_PROFILE_ID,
        expected_permission_id="permission-1",
        expected_email="me@example.com",
    )
    service.sync(DEFAULT_PROFILE_ID)
    reads, writes = gateway.review_reads, gateway.writes
    gateway.binding = WorkbookBinding(
        "00000000-0000-0000-0000-000000000099", WORKBOOK_SCHEMA_VERSION, "different_123"
    )
    with pytest.raises(WorkbookOwnershipError):
        service.sync(DEFAULT_PROFILE_ID)
    assert gateway.review_reads == reads
    assert gateway.writes == writes


def test_remote_write_failure_keeps_decision_and_next_run_converges(
    tmp_path: Path, clean_database
) -> None:
    with session_scope(clean_database) as session:
        observation, _ = add_observation(session, DEFAULT_PROFILE_ID)
        observation_id = observation.id
    gateway = FakeGateway()
    service = _service(tmp_path, clean_database, gateway)
    service.configure(
        DEFAULT_PROFILE_ID,
        expected_permission_id="permission-1",
        expected_email="me@example.com",
    )
    service.sync(DEFAULT_PROFILE_ID)
    remote = list(gateway.review_rows[1])
    remote[12] = "approve"
    gateway.review_rows = (gateway.review_rows[0], tuple(remote))
    gateway.fail_next_write = True
    with pytest.raises(SheetsSyncFailure) as captured:
        service.sync(DEFAULT_PROFILE_ID)
    assert captured.value.safe_code == "sheets_sync_failed"
    with session_scope(clean_database) as session:
        assert (
            session.get_one(LabObservation, observation_id).status
            == ReviewStatus.VERIFIED
        )
    recovered = service.sync(DEFAULT_PROFILE_ID)
    assert recovered.decisions_replayed == 1
    assert len(gateway.review_rows) == 1
    with session_scope(clean_database) as session:
        statuses = session.scalars(
            select(SheetsSyncRun.status).order_by(SheetsSyncRun.started_at)
        ).all()
    assert statuses == ["succeeded", "failed", "succeeded"]
