"""Database-backed, profile-scoped medical workflow adapter for the panel."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from health_agent.reminders.models import Reminder
from health_agent.reminders.repository import ReminderNotFound, ReminderRepository
from health_agent.reminders.time import parse_local_datetime
from health_agent.visits.models import Visit, VisitNote
from health_agent.visits.preparation import prepare_visit
from health_agent.visits.repository import VisitRepository
from health_agent.visits.telegram import render_brief


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    visits: tuple[Visit, ...]
    notes: tuple[tuple[str, tuple[VisitNote, ...]], ...]
    reminders: tuple[Reminder, ...]
    calendar: tuple[tuple[str, str], ...] = ()
    calendar_connection: str = ""


class DatabaseWorkflowAdapter:
    def __init__(self, sessions: Callable[[], AbstractContextManager[Session]], publication=None) -> None:
        self._sessions = sessions
        self._publication = publication

    def snapshot(self, profile_id: UUID) -> WorkflowSnapshot:
        with self._sessions() as session:
            visit_repo = VisitRepository(session)
            visits = visit_repo.list(profile_id, limit=20)
            notes = tuple(
                (visit.public_code, visit_repo.notes(profile_id, visit.public_code))
                for visit in visits
            )
            reminders = ReminderRepository(session).active(profile_id, limit=20)
        calendar: tuple[tuple[str, str], ...] = ()
        connection = "Calendar не настроен. Публикация только по явному выбору."
        if self._publication is not None:
            from health_agent.google_calendar.composition import CalendarStatusReader
            calendar = tuple((visit.public_code, self._publication.snapshot(profile_id, visit.public_code).status) for visit in visits)
            try:
                connection = CalendarStatusReader(self._publication).cards(profile_id)[0].detail
            except Exception:  # noqa: BLE001
                connection = "Локальный статус Calendar недоступен."
        return WorkflowSnapshot(visits, notes, reminders, calendar, connection)

    def action(self, profile_id: UUID, fields: Mapping[str, str]) -> str:
        operation = fields["operation"]
        identity = fields["action_id"]
        if not identity or len(identity) > 200:
            raise ValueError("invalid_action_identity")
        key = f"panel:{profile_id}:{identity}"
        if operation == "visit_calendar":
            from health_agent.google_calendar.publication import publication_notice
            if self._publication is None:
                raise ValueError("calendar_unavailable")
            return publication_notice(self._publication.publish(profile_id, fields["code"])) or "Calendar: без изменений."
        with self._sessions() as session:
            notice = _apply(session, profile_id, operation, fields, key)
        if self._publication is not None and operation in {"visit_question", "visit_answer", "visit_done", "visit_cancel", "visit_move", "visit_prepare"}:
            from health_agent.google_calendar.publication import publication_notice
            try:
                notice += " " + publication_notice(self._publication.sync_visit(profile_id, fields["code"]))
            except Exception:  # noqa: BLE001
                notice += " Calendar: синхронизация отложена; локальные изменения сохранены."
        return notice


def _apply(
    session: Session,
    profile_id: UUID,
    operation: str,
    fields: Mapping[str, str],
    key: str,
) -> str:
    visits, reminders = VisitRepository(session), ReminderRepository(session)
    if operation == "visit_create":
        start = parse_local_datetime(fields["when"], "Europe/Moscow")
        visit = visits.create(
            profile_id,
            title=fields["title"],
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            timezone_name="Europe/Moscow",
            creation_key=key,
        )
        return f"Визит сохранён локально: {visit.public_code}. Calendar автоматически не опубликован."
    if operation in {"visit_question", "visit_answer"}:
        visits.add_note(
            profile_id,
            fields["code"],
            kind="question" if operation.endswith("question") else "answer",
            text=fields["text"],
            action_key=key,
        )
        return "Запись сохранена."
    if operation == "visit_prepare":
        return render_brief(prepare_visit(session, profile_id, fields["code"]))
    if operation == "visit_move":
        visit = visits.get(profile_id, fields["code"])
        start = parse_local_datetime(fields["when"], visit.timezone_name)
        visits.reschedule(profile_id, fields["code"], starts_at=start, ends_at=start + (visit.ends_at - visit.starts_at), timezone_name=visit.timezone_name)
        return "Время визита сохранено."
    if operation in {"visit_done", "visit_cancel"}:
        method = visits.complete if operation.endswith("done") else visits.cancel
        method(profile_id, fields["code"])
        return "Статус визита сохранён."
    if operation == "reminder_create":
        due = parse_local_datetime(fields["when"], "Europe/Moscow")
        unit = fields.get("repeat_unit") or None
        every_text = fields.get("repeat_every") or ""
        every = int(every_text) if unit else None
        code = "p-" + hashlib.sha256(key.encode()).hexdigest()[:20]
        try:
            reminder = reminders.get(profile_id, code)
        except ReminderNotFound:
            reminder = reminders.propose(
                profile_id=profile_id,
                title=fields["title"],
                reason="Создано пользователем в локальной панели",
                source_type="user",
                source_reference="panel",
                due_at=due,
                timezone_name="Europe/Moscow",
                repeat_unit=unit,
                repeat_every=every,
                public_code=code,
            )
        if (
            reminder.title != fields["title"].strip()
            or reminder.due_at != due
            or reminder.repeat_unit != unit
            or reminder.repeat_every != every
        ):
            raise ValueError("reminder_action_conflict")
        return f"Предложение напоминания создано: {reminder.public_code}."
    if operation in {"reminder_confirm", "reminder_done", "reminder_cancel"}:
        if operation == "reminder_confirm":
            reminders.confirm(profile_id, fields["code"], action_key=key)
        elif operation == "reminder_done":
            reminders.complete(profile_id, fields["code"], action_key=key)
        else:
            reminders.cancel(profile_id, fields["code"], action_key=key)
        return "Статус напоминания сохранён."
    raise ValueError("unknown_workflow_operation")
