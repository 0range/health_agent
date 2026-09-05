from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionJob, LabExtractionProfile
from health_agent.models import (
    Document,
    DocumentPage,
    DocumentSourceRecord,
    LabObservation,
    Profile,
    ReviewStatus,
    SourceRecord,
)
from health_agent.panel.healthcheck import HealthcheckReader
from health_agent.panel.http import PanelApplication
from health_agent.panel.models import (
    ConnectorCard,
    DataCoverage,
    HealthcheckProfile,
    HealthcheckSnapshot,
    ProfilePanel,
    ProfileSummary,
)
from health_agent.panel.service import PanelService, SqlAlchemyProfileRepository
from health_agent.whoop.models import WhoopConnection, WhoopCycle, WhoopRawRecord

FIRST = UUID("10000000-0000-0000-0000-000000000001")
SECOND = UUID("20000000-0000-0000-0000-000000000002")
CHECKED = datetime(2026, 9, 5, 9, 30, tzinfo=UTC)


class SyntheticHealthService:
    """Two isolated profiles; no provider or mutation methods are reachable."""

    def __init__(self) -> None:
        self.healthcheck_reads = 0

    def healthcheck(self) -> HealthcheckSnapshot:
        self.healthcheck_reads += 1
        first = ProfilePanel(
            ProfileSummary(FIRST, "Профиль <A>"),
            (
                ConnectorCard(
                    "whoop",
                    "ready",
                    "Локальная синхронизация завершена.",
                    datetime(2026, 9, 5, 8, tzinfo=UTC),
                ),
            ),
        )
        second = ProfilePanel(
            ProfileSummary(SECOND, "Профиль B"),
            (ConnectorCard("whoop", "not_connected", "WHOOP не подключён."),),
        )
        return HealthcheckSnapshot(
            CHECKED,
            (
                HealthcheckProfile(
                    first,
                    DataCoverage(
                        "available",
                        latest_whoop_date=date(2026, 9, 3),
                        latest_lab_collected_date=date(2026, 8, 20),
                        latest_lab_issued_date=date(2026, 8, 21),
                        latest_received_at=datetime(2026, 8, 25, 10, tzinfo=UTC),
                        pending_extraction_count=2,
                        needs_review_count=3,
                        verified_count=7,
                    ),
                ),
                HealthcheckProfile(second, DataCoverage("empty")),
            ),
        )


def _get(service: SyntheticHealthService, path: str = "/healthcheck"):
    return PanelApplication(service, csrf_token="fixed").handle(  # type: ignore[arg-type]
        "GET", path, {"Host": "127.0.0.1:8766"}, b""
    )


def test_healthcheck_renders_isolated_two_profile_coverage_and_escapes_html() -> None:
    service = SyntheticHealthService()
    response = _get(service)
    html = response.body.decode()

    assert response.status == 200
    assert "Профиль &lt;A&gt;" in html
    assert "Профиль <A>" not in html
    assert "2026-09-03" in html
    assert "2026-09-05T08:00:00" in html
    assert "2026-08-20" in html and "2026-08-21" in html
    assert "Ожидают извлечения: 2" in html
    assert "Требуют проверки: 3" in html
    assert "Проверены: 7" in html
    second = html.split("Профиль B", 1)[1]
    assert "2026-09-03" not in second
    assert "WHOOP не подключён" in second
    assert "Нет данных" in second
    assert service.healthcheck_reads == 1


def test_healthcheck_unknown_is_not_healthy_and_leaks_no_raw_error() -> None:
    service = SyntheticHealthService()
    snapshot = service.healthcheck()
    broken = HealthcheckSnapshot(
        snapshot.checked_at,
        (
            HealthcheckProfile(
                ProfilePanel(
                    ProfileSummary(FIRST, "Безопасный профиль"),
                    (
                        ConnectorCard(
                            "database",
                            "status_unavailable",
                            "Локальный статус недоступен.",
                            error_code="local_status_unavailable",
                        ),
                    ),
                ),
                DataCoverage("unknown"),
            ),
        ),
    )
    service.healthcheck = lambda: broken  # type: ignore[method-assign]

    html = _get(service).body.decode()

    assert "Неизвестно" in html
    assert "Всё работает" not in html
    assert "password=" not in html and "token=" not in html


def test_healthcheck_handles_no_profiles_and_reuses_host_method_checks() -> None:
    service = SyntheticHealthService()
    service.healthcheck = lambda: HealthcheckSnapshot(CHECKED, ())  # type: ignore[method-assign]
    app = PanelApplication(service, csrf_token="fixed")  # type: ignore[arg-type]

    empty = app.handle("GET", "/healthcheck", {"Host": "127.0.0.1:8766"}, b"")
    bad_host = app.handle("GET", "/healthcheck", {"Host": "example.test"}, b"")
    post = app.handle("POST", "/healthcheck", {"Host": "127.0.0.1:8766"}, b"")

    assert empty.status == 200 and "Профилей пока нет" in empty.body.decode()
    assert bad_host.status == 400
    assert post.status == 405 and post.headers["Allow"] == "GET"


def test_healthcheck_database_failure_is_safe_503() -> None:
    service = SyntheticHealthService()

    def fail() -> HealthcheckSnapshot:
        raise RuntimeError("password=secret provider response")

    service.healthcheck = fail  # type: ignore[method-assign]
    response = _get(service)

    assert response.status == 503
    assert "временно недоступно" in response.body.decode()
    assert "password=" not in response.body.decode()


def test_reader_aggregates_real_rows_and_isolates_two_profiles(
    clean_database,
) -> None:
    second = SECOND
    with session_scope(clean_database) as database:
        database.add_all(
            (
                Profile(id=FIRST, name="Профиль A"),
                Profile(id=second, name="Профиль B"),
            )
        )
        database.flush()
        _add_whoop_day(database, FIRST, date(2026, 9, 3), "first")
        _add_whoop_day(database, second, date(2026, 7, 2), "second")
        _add_lab_document(
            database,
            FIRST,
            identity="1",
            collected=date(2026, 8, 20),
            issued=date(2026, 8, 21),
            received=datetime(2026, 8, 25, tzinfo=UTC),
            statuses=(ReviewStatus.NEEDS_REVIEW, ReviewStatus.VERIFIED),
            queued=True,
        )
        # A newer non-lab medical document must affect only the generic received
        # date, never the latest analysis collection/issue dates.
        _add_document(
            database,
            FIRST,
            identity="2",
            document_type="medical_note",
            collected=date(2026, 9, 4),
            issued=date(2026, 9, 4),
            received=datetime(2026, 9, 5, tzinfo=UTC),
        )
        _add_lab_document(
            database,
            second,
            identity="3",
            collected=date(2026, 6, 10),
            issued=date(2026, 6, 11),
            received=datetime(2026, 6, 12, tzinfo=UTC),
            statuses=(ReviewStatus.NEEDS_REVIEW,),
            queued=False,
        )

    reader = HealthcheckReader(lambda: session_scope(clean_database))
    first = reader.coverage(FIRST)
    other = reader.coverage(second)

    assert first.latest_whoop_date == date(2026, 9, 3)
    assert first.latest_lab_collected_date == date(2026, 8, 20)
    assert first.latest_lab_issued_date == date(2026, 8, 21)
    assert first.latest_received_at == datetime(2026, 9, 5, tzinfo=UTC)
    assert (first.pending_extraction_count, first.needs_review_count, first.verified_count) == (1, 1, 1)
    assert other.latest_whoop_date == date(2026, 7, 2)
    assert other.latest_lab_collected_date == date(2026, 6, 10)
    assert (other.pending_extraction_count, other.needs_review_count, other.verified_count) == (0, 1, 0)


def test_production_route_executes_read_only_local_sql(clean_database) -> None:
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):  # type: ignore[no-untyped-def]
        statements.append(statement.strip())

    event.listen(clean_database, "before_cursor_execute", capture)
    try:
        sessions = lambda: session_scope(clean_database)
        service = PanelService(
            SqlAlchemyProfileRepository(sessions),
            healthcheck_reader=HealthcheckReader(sessions),
            clock=lambda: CHECKED,
        )
        response = PanelApplication(service, csrf_token="fixed").handle(
            "GET", "/healthcheck", {"Host": "127.0.0.1:8766"}, b""
        )
    finally:
        event.remove(clean_database, "before_cursor_execute", capture)

    assert response.status == 200
    assert statements
    assert all(statement.upper().startswith("SELECT") for statement in statements)


def _add_whoop_day(
    database: Session, profile_id: UUID, local_day: date, identity: str
) -> None:
    connection = WhoopConnection(
        profile_id=profile_id,
        account_name=identity,
        auth_status="connected",
        granted_scopes=[],
    )
    database.add(connection)
    database.flush()
    raw = WhoopRawRecord(
        profile_id=profile_id,
        connection_id=connection.id,
        resource_kind="cycle",
        external_id=identity,
        payload_sha256=identity[0] * 64,
        payload={},
        source_updated_at=datetime.combine(local_day, datetime.min.time(), UTC),
        fetched_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    database.add(raw)
    database.flush()
    database.add(
        WhoopCycle(
            profile_id=profile_id,
            connection_id=connection.id,
            resource_kind="cycle",
            external_id=identity,
            start_at=datetime.combine(local_day, datetime.min.time(), UTC),
            local_day=local_day,
            raw_record_id=raw.id,
            source_values={},
        )
    )


def _add_document(
    database: Session,
    profile_id: UUID,
    *,
    identity: str,
    document_type: str,
    collected: date,
    issued: date,
    received: datetime,
) -> Document:
    source = SourceRecord(
        profile_id=profile_id,
        provider="synthetic",
        external_id=identity,
        revision=identity,
        received_at=received,
    )
    document = Document(
        profile_id=profile_id,
        sha256=identity * 64,
        vault_path=f"synthetic/{identity}",
        media_type="application/pdf",
        document_type=document_type,
        collected_date=collected,
        issued_date=issued,
        processing_status="processed",
    )
    database.add_all((source, document))
    database.flush()
    database.add(
        DocumentSourceRecord(
            document_id=document.id,
            source_record_id=source.id,
            profile_id=profile_id,
        )
    )
    return document


def _add_lab_document(
    database: Session,
    profile_id: UUID,
    *,
    identity: str,
    collected: date,
    issued: date,
    received: datetime,
    statuses: tuple[ReviewStatus, ...],
    queued: bool,
) -> None:
    document = _add_document(
        database,
        profile_id,
        identity=identity,
        document_type="laboratory_report",
        collected=collected,
        issued=issued,
        received=received,
    )
    page = DocumentPage(
        document=document,
        page_number=1,
        extracted_text=None,
        extraction_method="synthetic",
    )
    database.add(page)
    database.flush()
    for index, status in enumerate(statuses):
        verified = status == ReviewStatus.VERIFIED
        database.add(
            LabObservation(
                document=document,
                page_number=1,
                canonical_name=f"synthetic_{index}",
                source_name="synthetic",
                source_value="synthetic",
                parsed_value=0 if verified else None,
                normalized_value=0 if verified else None,
                normalized_unit="synthetic" if verified else None,
                evidence_excerpt="synthetic",
                confidence=0,
                status=status,
            )
        )
    if queued:
        database.add(LabExtractionProfile(profile_id=profile_id))
        database.flush()
        database.add(
            LabExtractionJob(
                profile_id=profile_id,
                document_id=document.id,
                page_number=1,
                status="queued",
            )
        )
