"""Deterministic visit commands for an already authenticated Telegram context."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from health_agent.db import session_scope
from health_agent.reminders.time import parse_local_datetime
from health_agent.telegram.types import MessageContext
from health_agent.visits.models import Visit, VisitNote
from health_agent.visits.preparation import VisitBrief, prepare_visit
from health_agent.visits.repository import VisitNotFound, VisitRepository

VISIT_COMMANDS = frozenset(
    {
        "visits",
        "visit_new",
        "visit",
        "visit_prepare",
        "visit_question",
        "visit_answer",
        "visit_done",
        "visit_cancel",
        "visit_move",
        "visit_calendar",
    }
)
_LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def parse_visit_time(value: str, timezone_name: str) -> datetime:
    """Require a date and minute precision; reject ambiguous/nonexistent time."""
    if _LOCAL_TIME.fullmatch(value) is None:
        raise ValueError("invalid_visit_time")
    return parse_local_datetime(value, timezone_name)


class DatabaseVisitCommands:
    usage_text = "Формат: /visit_new YYYY-MM-DDTHH:MM | название. Список: /visits. Действия: /visit КОД."
    unavailable_text = "Визит не найден или действие недоступно."

    def __init__(self, engine: Engine, publication=None) -> None:
        self._engine = engine
        self._publication = publication

    def handle(self, context: MessageContext, text: str) -> str | None:
        from health_agent.google_calendar.publication import publication_notice

        pieces = text.strip().split(maxsplit=1)
        name = pieces[0].split("@", 1)[0].casefold() if pieces else ""
        arguments = pieces[1].strip() if len(pieces) == 2 else ""
        if name == "/visit_calendar":
            if len(arguments.split()) != 1:
                return "Формат: /visit_calendar КОД"
            if self._publication is None:
                return "Calendar недоступен; визит остаётся локальным."
            try:
                result = self._publication.publish(context.profile_id, arguments)
                return publication_notice(result) or "Calendar: публикация уже подтверждена."
            except VisitNotFound:
                return self.unavailable_text
            except Exception:  # noqa: BLE001 - never echo local/network details.
                return "Calendar недоступен; локальный визит сохранён. Повторите публикацию."
        response = self._handle_local(context, text)
        if (self._publication is not None and name in {
            "/visit_question", "/visit_answer", "/visit_move", "/visit_done", "/visit_cancel", "/visit_prepare"
        } and response is not None and response not in {
            self.usage_text, self.unavailable_text,
            "Не удалось сохранить или прочитать визит. Попробуйте позже."
        }):
            # _handle_local's session_scope has exited and committed.
            try:
                notice = publication_notice(self._publication.sync_visit(context.profile_id, arguments.split()[0]))
            except Exception:  # noqa: BLE001
                notice = "Calendar: повторная синхронизация отложена; локальные изменения сохранены."
            if notice:
                response += "\n" + notice
        return response

    def _handle_local(self, context: MessageContext, text: str) -> str | None:
        pieces = text.strip().split(maxsplit=1)
        if not pieces or not pieces[0].startswith("/"):
            return None
        name = pieces[0][1:].split("@", 1)[0].casefold()
        if name not in VISIT_COMMANDS:
            return None
        if len(text) > 12000:
            return self.usage_text
        arguments = pieces[1].strip() if len(pieces) == 2 else ""
        key = f"telegram:{context.bot_id}:{context.update_id}"
        try:
            with session_scope(self._engine) as session:
                repo = VisitRepository(session)
                profile = context.profile_id
                if name == "visits":
                    if arguments:
                        raise ValueError("invalid_visit_command")
                    visits = repo.list(profile)
                    return bounded_message(
                        "\n".join(
                            [
                                "Визиты (до 20):",
                                *[summary(visit) for visit in visits],
                                "Создать: /visit_new YYYY-MM-DDTHH:MM | название",
                            ]
                        )
                    )
                if name == "visit_new":
                    when, separator, title = arguments.partition("|")
                    if not separator:
                        raise ValueError("invalid_visit_command")
                    start = parse_visit_time(when.strip(), "Europe/Moscow")
                    visit = repo.create(
                        profile,
                        title=title.strip(),
                        starts_at=start,
                        ends_at=start + timedelta(hours=1),
                        timezone_name="Europe/Moscow",
                        creation_key=key,
                    )
                    return render_visit(visit, ())
                if name in {"visit_question", "visit_answer"}:
                    code, separator, note = arguments.partition(" ")
                    if not separator:
                        raise ValueError("invalid_visit_command")
                    repo.add_note(
                        profile,
                        code,
                        kind="question" if name == "visit_question" else "answer",
                        text=note,
                        action_key=key,
                    )
                    return f"{'Вопрос' if name == 'visit_question' else 'Записанный ответ'} сохранён. /visit {code}"
                if name == "visit_move":
                    move = arguments.split()
                    if len(move) != 2:
                        raise ValueError("invalid_visit_command")
                    visit = repo.get(profile, move[0])
                    start = parse_visit_time(move[1], visit.timezone_name)
                    moved = repo.reschedule(
                        profile,
                        move[0],
                        starts_at=start,
                        ends_at=start + (visit.ends_at - visit.starts_at),
                        timezone_name=visit.timezone_name,
                    )
                    return render_visit(moved, repo.notes(profile, moved.public_code))
                if len(arguments.split()) != 1:
                    raise ValueError("invalid_visit_command")
                if name == "visit_prepare":
                    return render_brief(prepare_visit(session, profile, arguments))
                visit = (
                    repo.complete(profile, arguments)
                    if name == "visit_done"
                    else repo.cancel(profile, arguments)
                    if name == "visit_cancel"
                    else repo.get(profile, arguments)
                )
                return render_visit(visit, repo.notes(profile, arguments))
        except VisitNotFound:
            return self.unavailable_text
        except (ValueError, OverflowError):
            return self.usage_text
        except SQLAlchemyError:
            return "Не удалось сохранить или прочитать визит. Попробуйте позже."


def bounded_message(value: str, limit: int = 12000) -> str:
    return (
        value
        if len(value) <= limit
        else value[: limit - 45] + "\n[Показана часть записей: достигнут лимит.]"
    )


def summary(visit: Visit) -> str:
    local = visit.starts_at.astimezone(ZoneInfo(visit.timezone_name))
    status = {
        "planned": "запланирован",
        "completed": "завершён",
        "cancelled": "отменён",
    }[visit.status]
    return f"{visit.title} — {local:%Y-%m-%d %H:%M} ({visit.timezone_name}), {status}. /visit {visit.public_code}"


def render_visit(visit: Visit, notes: tuple[VisitNote, ...]) -> str:
    code = visit.public_code
    lines = [summary(visit)]
    if visit.status != "cancelled":
        lines.extend(
            [
                f"Подготовиться: /visit_prepare {code}",
                f"Вопрос: /visit_question {code} текст",
                f"Записать ответ: /visit_answer {code} текст",
                f"Опубликовать в Calendar: /visit_calendar {code}",
            ]
        )
    if visit.status == "planned":
        lines.extend(
            [
                f"Завершить: /visit_done {code}",
                f"Отменить: /visit_cancel {code}",
                f"Перенести: /visit_move {code} YYYY-MM-DDTHH:MM",
            ]
        )
    if visit.source_document_id:
        lines.append(f"Источник: document:{visit.source_document_id}")
    lines.extend(
        f"{'Вопрос' if note.kind == 'question' else 'Записанный ответ'}: {note.text}"
        for note in notes
    )
    return bounded_message("\n".join(lines))


def render_brief(brief: VisitBrief) -> str:
    lines = [
        bounded_message(
            render_visit(brief.visit, brief.questions + brief.answers), 6000
        ),
        "Вопросы для общего обсуждения с врачом; ответы — ваши записи.",
        f"Непроверенных показателей, исключённых из подготовки: {brief.pending_count}.",
        "Проверенные датированные анализы (до 10, исходные значения):",
    ]
    for item in brief.observations:
        lines.append(
            f"{item.observed_on}: {item.source_name} = {item.source_value} {item.source_unit or ''}; референс: {item.reference_text if item.reference_text is not None else 'не указан'}. {item.source_reference}"
        )
    return bounded_message("\n".join(lines))
