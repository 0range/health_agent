"""Connected v0.1 journeys: synthetic PDFs/Postgres and in-process gateways only."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import pymupdf
import pytest
import requests
from google_calendar.test_adapter import FakeGateway as CalendarGateway
from google_calendar.test_adapter import FakeOAuth, configured
from reminders.test_dispatcher import FakeGateway as TelegramGateway
from reminders.test_dispatcher import _messenger
from sqlalchemy import func, select, text
from sqlalchemy.exc import NoResultFound

from health_agent.db import session_scope
from health_agent.google_calendar.publication import CalendarPublicationService
from health_agent.google_calendar.service import CalendarService, event_id
from health_agent.google_sheets.projection import build_projection
from health_agent.google_sheets.types import WorkbookBinding
from health_agent.importer import approve_observation, import_document
from health_agent.insights.models import SignalKind
from health_agent.lab_dashboard import LabSeries, lab_card_specs
from health_agent.models import (
    Document,
    LabObservation,
    PageEvidence,
    Profile,
    ReviewStatus,
)
from health_agent.questions.context import HealthContextBuilder
from health_agent.questions.openai import build_responder_input
from health_agent.questions.reports import read_reports
from health_agent.questions.service import (
    INSUFFICIENT_EVIDENCE_TEXT,
    HealthQuestionApplicationService,
)
from health_agent.reminders.dispatcher import ReminderDispatcher
from health_agent.reminders.models import HealthReminder, ReminderStatus
from health_agent.reminders.repository import ReminderNotFound, ReminderRepository
from health_agent.reminders.telegram import DatabaseReminderCommands
from health_agent.telegram.types import MessageContext
from health_agent.vault import FileVault
from health_agent.visits.repository import VisitNotFound, VisitRepository
from health_agent.visits.telegram import DatabaseVisitCommands
from health_agent.whoop.dashboard import whoop_card_specs
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)

OWNER, OTHER = UUID(int=1), UUID(int=2)
# Include records written during the journey without treating import time as a lab date.
NOW = datetime.now(UTC) + timedelta(minutes=5)
MEDICAL_DATE = (NOW - timedelta(days=1)).date()
RECOMMENDATION = "Recommendations: Discuss the recorded result at the next visit."


@pytest.fixture(autouse=True)
def prohibit_http(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Journey tests must never call an external HTTP service")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    yield
    assert attempts == [], (
        "Even an internally caught external HTTP attempt is forbidden"
    )


def _pdf(path, *, rows=None, narrative=None):
    """Real physical grid, intentionally column-major PDF text insertion."""
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=300)
        if rows:
            xs = [30, 190, 280, 390, 480, 570]
            ys = [30 + 32 * index for index in range(len(rows) + 2)]
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
            values = [["Test", "Result", "Reference range", "Unit", "Comment"], *rows]
            for column in range(5):
                for row, values_row in enumerate(values):
                    if values_row[column]:
                        page.insert_text(
                            (xs[column] + 3, ys[row] + 19),
                            values_row[column],
                            fontsize=7,
                        )
        if narrative:
            page.insert_text((35, 220), narrative, fontsize=8)
        pdf.save(path)
    return path


@pytest.fixture
def archive(clean_database, tmp_path):
    vault = FileVault(tmp_path / "vault")
    source = _pdf(
        tmp_path / "synthetic-lab.pdf",
        rows=[
            ["Glucose", "5.10", "3.9-5.5", "mmol/L", "synthetic"],
            ["Hemoglobin", "145 146", "130-170", "g/L", "ambiguous"],
        ],
    )
    pending_source = _pdf(
        tmp_path / "synthetic-pending.pdf",
        rows=[["Glucose", "7.70", "3.9-5.5", "mmol/L", "unreviewed"]],
    )
    report_source = _pdf(tmp_path / "synthetic-report.pdf", narrative=RECOMMENDATION)
    foreign_source = _pdf(
        tmp_path / "synthetic-foreign.pdf",
        rows=[["Glucose", "99.90", "3.9-5.5", "mmol/L", "foreign"]],
        narrative="Conclusion: FOREIGN PRIVATE REPORT",
    )
    with session_scope(clean_database) as session:
        session.add(Profile(id=OTHER, name="Synthetic other"))
        session.flush()
        imported = import_document(
            session, vault, source, None, collected_date=MEDICAL_DATE
        )
        pending = import_document(
            session, vault, pending_source, None, collected_date=MEDICAL_DATE
        )
        report = import_document(
            session, vault, report_source, None, issued_date=MEDICAL_DATE
        )
        foreign = import_document(
            session,
            vault,
            foreign_source,
            None,
            profile_id=OTHER,
            collected_date=MEDICAL_DATE,
        )
        foreign_row = session.scalar(
            select(LabObservation).where(
                LabObservation.document_id == foreign.document_id
            )
        )
        approve_observation(session, foreign_row.id, profile_id=OTHER)
        row_id = session.scalar(
            select(LabObservation.id).where(
                LabObservation.document_id == imported.document_id
            )
        )
    return SimpleNamespace(
        engine=clean_database,
        vault=vault,
        source=source,
        imported=imported,
        pending=pending,
        report=report,
        foreign=foreign,
        row_id=row_id,
    )


def _context(session, profile=OWNER):
    return HealthContextBuilder(session, clock=lambda: NOW).build(
        profile, "Что показывают анализы?"
    )


def _approve(archive):
    with session_scope(archive.engine) as session:
        approve_observation(session, archive.row_id, profile_id=OWNER)


def _message(update, profile=OWNER):
    return MessageContext(111, profile, 101, 101, update, update, NOW, NOW)


def test_incoming_pdf_review_to_factual_overview(archive):
    with session_scope(archive.engine) as session:
        pending_context = _context(session)
        assert pending_context.evidence == ()
        assert not [
            signal
            for signal in pending_context.snapshot.signals
            if signal.kind == SignalKind.LAB and signal.value is not None
        ]
        assert (
            archive.imported.candidate_count == 1
        )  # Ambiguous physical row is not guessed.
        observation = session.get(LabObservation, archive.row_id)
        assert observation.status is ReviewStatus.NEEDS_REVIEW
        proof = session.get(PageEvidence, observation.page_evidence_id)
        assert proof.document_id == archive.imported.document_id
        assert session.get(Document, proof.document_id).profile_id == OWNER
        assert (
            proof.source_sha256
            == hashlib.sha256(archive.source.read_bytes()).hexdigest()
        )
        assert proof.evidence_json["rows"][0]["result"]["text"] == "5.10"
        assert len(proof.evidence_json["rows"][0]["result"]["bbox"]) == 4
        with pytest.raises(NoResultFound):
            approve_observation(session, observation.id, profile_id=OTHER)
        approve_observation(session, observation.id, profile_id=OWNER)
        context = _context(session)
        assert [
            (item.source_value, item.source_unit, item.source_reference)
            for item in context.evidence
        ] == [("5.10", "mmol/L", "3.9-5.5")]
        assert context.evidence[0].observed_at.date() == MEDICAL_DATE
        signals = [
            signal
            for signal in context.snapshot.signals
            if signal.kind == SignalKind.LAB and signal.value is not None
        ]
        assert len(signals) == 1
        assert (signals[0].value, signals[0].unit, signals[0].reference) == (
            "5.10",
            "mmol/L",
            "3.9-5.5",
        )
        assert str(archive.row_id) in {
            citation.source_id for citation in signals[0].citations
        }
        assert signals[0].citations[0].page_number == 1
        assert "99.90" not in repr(context) and "FOREIGN" not in repr(context)
        count = session.scalar(select(func.count()).select_from(LabObservation))
        duplicate = import_document(session, archive.vault, archive.source, None)
        assert (
            duplicate.status == "duplicate"
            and duplicate.document_id == archive.imported.document_id
        )
        assert session.scalar(select(func.count()).select_from(LabObservation)) == count


class AnswerGateway:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def respond(self, **kwargs):
        self.calls.append(kwargs)
        context = kwargs["context"]
        if self.answer is not None:
            return self.answer
        lab, report = context.evidence[0], context.reports[0]
        return (
            f"В источнике глюкоза {lab.source_value} {lab.source_unit} {lab.citation_label}. "
            f"В документе рекомендовано обсудить результат на приёме {report.citation_label}. "
            "Это не диагноз; медицинский срок повторения в документе не указан."
        )


def test_health_question_has_separate_attributed_lab_and_report(archive):
    _approve(archive)
    gateway = AnswerGateway()
    with session_scope(archive.engine) as session:
        service = HealthQuestionApplicationService(
            HealthContextBuilder(session, clock=lambda: NOW), gateway
        )
        result = service.answer(
            OWNER, "Что показывают анализы и что обсудить с врачом?"
        )
        received = gateway.calls[0]["context"]
        assert gateway.calls[0]["profile_id"] == OWNER
        assert len(received.evidence) == len(received.reports) == 1
        assert received.reports[0].kind == "document_excerpt"
        assert received.reports[0].text == RECOMMENDATION
        assert (
            received.reports[0].source_reference
            == f"document:{archive.report.document_id}#page=1"
        )
        payload = build_responder_input("Что обсудить?", received)[0]
        material = json.loads(payload["content"][1]["text"])["reported_material"]
        assert material[0]["kind"] == "document_excerpt"
        assert "verified_observations" not in material[0]
        assert "[LAB1]" not in result.text and "[DOC1]" not in result.text
        assert "Источники:" not in result.text
        assert str(archive.report.document_id) not in result.text
        assert "5.10 mmol/L" in result.text
        assert "Это не диагноз" in result.text and "срок повторения" in result.text
        assert "FOREIGN" not in repr(received) and "7.70" not in repr(received)


@pytest.mark.parametrize(
    "generated",
    ["Invented glucose 100 [LAB99].", "Invented glucose 100 without a citation."],
)
def test_unknown_and_uncited_answers_fail_closed(archive, generated):
    _approve(archive)
    gateway = AnswerGateway(generated)
    with session_scope(archive.engine) as session:
        result = HealthQuestionApplicationService(
            HealthContextBuilder(session, clock=lambda: NOW), gateway
        ).answer(OWNER, "Что показывают анализы?")
        assert len(gateway.calls) == 1
        assert result.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)
        assert "Invented" not in result.text


def test_source_free_question_does_not_call_responder_or_invent_labs(clean_database):
    gateway = AnswerGateway("Invented glucose 100 [LAB1].")
    with session_scope(clean_database) as session:
        result = HealthQuestionApplicationService(
            HealthContextBuilder(session, clock=lambda: NOW), gateway
        ).answer(OWNER, "Что показывают анализы?")
        assert result.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)
        assert result.evidence == () and gateway.calls == []
        assert "глюкоза" not in result.text and "100" not in result.text


def test_documented_next_action_explicit_reminder_delivery_and_recurrence(
    archive, tmp_path
):
    with session_scope(archive.engine) as session:
        recommendation = read_reports(session, OWNER, as_of=NOW)[0]
        assert recommendation.text == RECOMMENDATION
        assert session.scalar(select(func.count()).select_from(HealthReminder)) == 0
        # Date and recurrence are a synthetic USER choice, never inferred from the report.
        proposed = ReminderRepository(session).propose(
            profile_id=OWNER,
            title="Discuss synthetic report",
            reason=recommendation.text,
            source_type="doctor_note",
            source_reference=recommendation.source_reference,
            due_at=NOW,
            timezone_name="Europe/Moscow",
            repeat_unit="days",
            repeat_every=7,
            now=NOW,
            public_code="synthetic-next-action",
        )
        assert proposed.status is ReminderStatus.PENDING_CONFIRMATION
        assert ReminderRepository(session).due_occurrences(NOW) == ()
        with pytest.raises(ReminderNotFound):
            ReminderRepository(session).confirm(OTHER, proposed.public_code, now=NOW)
        foreign = ReminderRepository(session).propose(
            profile_id=OTHER,
            title="FOREIGN PRIVATE REMINDER",
            reason="Synthetic other user",
            source_type="user",
            source_reference="telegram",
            due_at=NOW,
            timezone_name="Europe/Moscow",
            now=NOW,
            public_code="foreign-reminder",
        )
        ReminderRepository(session).confirm(OTHER, foreign.public_code, now=NOW)
        ReminderRepository(session).mark_proposal_notified(
            OTHER, foreign.id, notified_at=NOW
        )
    gateway = TelegramGateway()
    dispatcher = ReminderDispatcher(
        archive.engine,
        _messenger(tmp_path / "telegram.sqlite", gateway),
        clock=lambda: NOW,
    )
    first = dispatcher.run()
    assert (first.proposals_sent, first.due_sent) == (1, 0)
    assert first.failed == 1  # The foreign profile has no authorized Telegram binding.
    commands = DatabaseReminderCommands(archive.engine, clock=lambda: NOW)
    assert "подтверждено" in commands.handle(
        _message(10), f"/reminder_confirm {proposed.public_code}"
    )
    commands.handle(_message(10), f"/reminder_confirm {proposed.public_code}")
    assert dispatcher.run().due_sent == 1
    assert dispatcher.run().due_sent == 0
    commands.handle(_message(11), f"/reminder_done {proposed.public_code}")
    commands.handle(_message(11), f"/reminder_done {proposed.public_code}")
    with session_scope(archive.engine) as session:
        repo = ReminderRepository(session)
        child = repo.successor(OWNER, proposed.public_code)
        assert child is not None and child.due_at == NOW + timedelta(days=7)
        assert child.status is ReminderStatus.SCHEDULED
        assert child.source_reference == recommendation.source_reference
        assert repo.get(OWNER, proposed.public_code).status is ReminderStatus.COMPLETED
        assert (
            session.scalar(
                select(func.count())
                .select_from(HealthReminder)
                .where(HealthReminder.profile_id == OWNER)
            )
            == 2
        )
        with pytest.raises(ReminderNotFound):
            repo.get(OTHER, child.public_code)
    assert dispatcher.run().due_sent == 0
    assert len(gateway.sent) == 2
    assert all(
        chat == 101 and "FOREIGN" not in message for chat, message in gateway.sent
    )
    assert "/reminder_confirm synthetic-next-action" in gateway.sent[0][1]
    assert "/reminder_done synthetic-next-action" in gateway.sent[1][1]
    assert recommendation.source_reference in gateway.sent[1][1]


def test_visit_preparation_answer_and_explicit_calendar_lifecycle(archive, tmp_path):
    _approve(archive)
    profiles, tokens = configured(tmp_path / "calendar", OWNER)
    committed_question_counts = []

    class CommittedCalendarGateway(CalendarGateway):
        def check_committed(self, body):
            if "description" not in body:
                return
            # Separate transaction at the API-write boundary must see the notes.
            with session_scope(archive.engine) as session:
                questions = [
                    note.text
                    for note in VisitRepository(session).notes(OWNER, code)
                    if note.kind == "question"
                ]
            assert all(question in body["description"] for question in questions)
            committed_question_counts.append(len(questions))

        def insert(self, calendar_id, body):
            self.check_committed(body)
            return super().insert(calendar_id, body)

        def patch(self, calendar_id, remote_id, body, etag):
            self.check_committed(body)
            return super().patch(calendar_id, remote_id, body, etag)

    gateway = CommittedCalendarGateway()
    calendar = CalendarService(profiles, tokens, FakeOAuth(), lambda _: gateway)
    publication = CalendarPublicationService(
        archive.engine, calendar, tmp_path / "locks"
    )
    commands = DatabaseVisitCommands(archive.engine, publication)
    start = (NOW + timedelta(days=2)).strftime("%Y-%m-%dT10:00")
    commands.handle(_message(20), f"/visit_new {start} | Synthetic visit")
    commands.handle(_message(20), f"/visit_new {start} | Synthetic visit")
    with session_scope(archive.engine) as session:
        visits = VisitRepository(session).list(OWNER)
        assert len(visits) == 1
        visit = visits[0]
    code = visit.public_code
    commands.handle(_message(21), f"/visit_prepare {code}")
    commands.handle(_message(22), f"/visit_answer {code} PRIVATE SAVED ANSWER")
    assert gateway.events == {}  # No publication merely because local notes changed.
    with session_scope(archive.engine) as session:
        before = _context(session)
        answers = [report for report in before.reports if report.kind == "visit_answer"]
        assert [answer.text for answer in answers] == ["PRIVATE SAVED ANSWER"]
        assert answers[0].source_reference.startswith(f"visit:{visit.id}#note=")
        assert len(before.evidence) == 1 and "PRIVATE" not in repr(before.evidence)
        assert "PRIVATE SAVED ANSWER" not in repr(_context(session, OTHER))
        payload = build_responder_input("Что обсудить?", before)[0]
        material = json.loads(payload["content"][1]["text"])["reported_material"]
        assert any(
            item["kind"] == "visit_answer" and item["text"] == "PRIVATE SAVED ANSWER"
            for item in material
        )
    commands.handle(_message(23), f"/visit_calendar {code}")
    remote_id = event_id(OWNER, visit.id)
    assert set(gateway.events) == {remote_id} and gateway.insert_count == 1
    with session_scope(archive.engine) as session:
        questions = [
            note.text
            for note in VisitRepository(session).notes(OWNER, code)
            if note.kind == "question"
        ]
        assert len(questions) == 5
        assert all(
            question in gateway.events[remote_id]["description"]
            for question in questions
        )
    commands.handle(
        _message(24), f"/visit_question {code} Additional synthetic question?"
    )
    commands.handle(
        _message(24), f"/visit_question {code} Additional synthetic question?"
    )
    assert "Additional synthetic question?" in gateway.events[remote_id]["description"]
    assert committed_question_counts == [5, 6]
    previous_start = gateway.events[remote_id]["start"]
    moved = (NOW + timedelta(days=3)).strftime("%Y-%m-%dT11:00")
    commands.handle(_message(25), f"/visit_move {code} {moved}")
    assert gateway.events[remote_id]["start"] != previous_start
    writes = (gateway.insert_count, gateway.patch_count)
    assert (
        commands.handle(_message(26, OTHER), f"/visit {code}")
        == commands.unavailable_text
    )
    assert (
        commands.handle(_message(27, OTHER), f"/visit_cancel {code}")
        == commands.unavailable_text
    )
    with pytest.raises(VisitNotFound):
        publication.publish(OTHER, code)
    assert (gateway.insert_count, gateway.patch_count) == writes
    commands.handle(_message(28), f"/visit_cancel {code}")
    assert gateway.events[remote_id]["status"] == "cancelled"
    assert set(gateway.events) == {remote_id} and gateway.insert_count == 1
    event_text = repr(gateway.events[remote_id])
    assert "PRIVATE SAVED ANSWER" not in event_text and "5.10" not in event_text
    assert "FOREIGN" not in event_text and "vault" not in event_text


def test_verified_lab_in_sheets_and_unit_specific_sql_keeps_whoop_separate(archive):
    with session_scope(archive.engine) as session:
        for profile, strain in ((OWNER, 12), (OTHER, 3)):
            connection = register_authorized_connection(
                session, profile, "synthetic", 101, ("read:cycles",)
            )
            payload = {
                "id": 101,
                "user_id": 101,
                "start": f"{MEDICAL_DATE.isoformat()}T07:00:00Z",
                "updated_at": f"{MEDICAL_DATE.isoformat()}T08:00:00Z",
                "timezone_offset": "+00:00",
                "score_state": "SCORED",
                "score": {"strain": strain},
            }
            store_normalized_record(
                session, connection, normalize_whoop("cycle", payload), payload, NOW
            )
        session.flush()
        whoop_query = next(
            spec.query
            for spec in whoop_card_specs(OWNER)
            if spec.metrics == ("strain",)
        )
        whoop_before = session.execute(text(whoop_query)).mappings().all()
        assert [row["strain"] for row in whoop_before] == [Decimal(12)]
        binding = WorkbookBinding(str(OWNER), "1", "synthetic-only")
        before = build_projection(session, OWNER, binding)
        assert before.workbook.sheets[0].rows == () and len(before.pending_reviews) == 2
        specs = lab_card_specs(OWNER, (LabSeries("glucose", "Глюкоза", "mmol/L"),))
        assert session.execute(text(specs[0].query)).all() == []
        approve_observation(session, archive.row_id, profile_id=OWNER)
        projection = build_projection(session, OWNER, binding)
        sheet = projection.workbook.sheets[0]
        assert sheet.title == "Lab history" and len(sheet.rows) == 1
        row = dict(zip(sheet.headers, sheet.rows[0], strict=True))
        assert (row["Source value"], row["Source unit"], row["Reference"]) == (
            "5.10",
            "mmol/L",
            "3.9-5.5",
        )
        assert row["Document ID"] == str(archive.imported.document_id)
        assert row["Medical date"] == MEDICAL_DATE.isoformat()
        assert len(projection.pending_reviews) == 1
        details = session.execute(text(specs[0].query)).mappings().all()
        history = session.execute(text(specs[1].query)).mappings().all()
        assert len(details) == len(history) == 1
        assert (
            details[0]["source_value"] == "5.10"
            and details[0]["source_unit"] == "mmol/L"
        )
        assert details[0]["reference_text"] == "3.9-5.5"
        assert details[0]["document_id"] == archive.imported.document_id
        assert details[0]["page_number"] == 1
        assert (
            history[0]["result"],
            history[0]["reference_low"],
            history[0]["reference_high"],
        ) == (Decimal("5.10"), Decimal("3.9"), Decimal("5.5"))
        assert history[0]["observation_id"] == archive.row_id
        assert session.execute(text(whoop_query)).mappings().all() == whoop_before
        foreign_projection = build_projection(
            session, OTHER, WorkbookBinding(str(OTHER), "1", "other-only")
        )
        assert foreign_projection.workbook.sheets[0].rows[0][5] == "99.90"
        assert "7.70" not in repr(sheet.rows) and "99.90" not in repr(history)
        assert (
            session.get(Document, archive.pending.document_id).processing_status
            == "needs_review"
        )
