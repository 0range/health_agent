from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from health_agent import cli
from health_agent.automation.registry import CalendarJobAdapter
from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.google_calendar import composition
from health_agent.google_calendar.composition import (
    CalendarStatusReader,
    build_calendar_service,
    build_publication_service,
)
from health_agent.google_calendar.models import CalendarResult
from health_agent.google_calendar.publication import VisitCalendarPublication
from health_agent.models import Profile
from health_agent.panel.http import PanelApplication
from health_agent.panel.service import PanelService, SqlAlchemyProfileRepository
from health_agent.panel.workflows import DatabaseWorkflowAdapter
from health_agent.staging import StagingConfigurationError, StagingEnvironment
from health_agent.telegram.types import MessageContext
from health_agent.visits import cli as visit_cli
from health_agent.visits.preparation import GENERAL_QUESTIONS
from health_agent.visits.repository import VisitRepository
from health_agent.visits.telegram import DatabaseVisitCommands

from .test_publication import OWNER, create_visit, publication


def context(update=1, profile=OWNER):
    return MessageContext(1, profile, 1, 1, update, update, None, datetime.now(UTC))


def test_telegram_publication_and_postcommit_edits(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    commands = DatabaseVisitCommands(clean_database, service)
    code = visit.public_code
    commands.handle(context(1), f"/visit_question {code} Local only")
    assert fake.calls == []
    assert "подтверждены" in commands.handle(context(2), f"/visit_calendar {code}")
    commands.handle(context(2), f"/visit_calendar {code}")
    assert len(fake.calls) == 1

    def callback(event):
        with session_scope(clean_database) as session:
            notes = VisitRepository(session).notes(OWNER, code)
            assert any(note.text == "New committed question" for note in notes)
        return CalendarResult("stable", "updated")

    fake.callback = callback
    commands.handle(context(3), f"/visit_question {code} New committed question")
    assert len(fake.calls) == 2
    commands.handle(context(3), f"/visit_question {code} New committed question")
    assert len(fake.calls) == 2
    commands.handle(context(4), f"/visit_answer {code} Private answer")
    assert len(fake.calls) == 2
    commands.handle(context(5), f"/visit_move {code} 2026-10-03T10:00")
    assert len(fake.calls) == 3 and fake.calls[-1].starts_at.day == 3
    commands.handle(context(6), f"/visit_cancel {code}")
    assert fake.calls[-1].cancelled
    assert "не найден" in commands.handle(
        context(7, uuid4()), f"/visit_calendar {code}"
    )


def test_telegram_failure_retains_note_and_retries(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    service.publish(OWNER, visit.public_code)
    fake.callback = lambda _: CalendarResult(
        "stable", "deferred", safe_error="permission_denied"
    )
    response = DatabaseVisitCommands(clean_database, service).handle(
        context(), f"/visit_question {visit.public_code} Keep this note"
    )
    assert "очереди" in response
    with session_scope(clean_database) as session:
        assert (
            VisitRepository(session).notes(OWNER, visit.public_code)[0].text
            == "Keep this note"
        )
    fake.callback = None
    assert service.sync_profile(OWNER)[0].status == "published"


def test_panel_get_readonly_csrf_and_foreign_code(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    sessions = lambda: session_scope(clean_database)
    panel = PanelService(
        SqlAlchemyProfileRepository(sessions),
        readers=[CalendarStatusReader(service)],
        workflows=DatabaseWorkflowAdapter(sessions, service),
    )
    app = PanelApplication(panel, csrf_token="csrf")
    path = f"/profiles/{OWNER}/medical"
    headers = {"Host": "127.0.0.1:8766", "Origin": "http://127.0.0.1:8766"}
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    sql_event.listen(clean_database, "before_cursor_execute", capture)
    try:
        assert app.handle("GET", path, headers, b"").status == 200
    finally:
        sql_event.remove(clean_database, "before_cursor_execute", capture)
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert fake.calls == []
    healthcheck = app.handle("GET", "/healthcheck", headers, b"")
    assert healthcheck.status == 200 and "Google Calendar" in healthcheck.body.decode()
    assert "Публикаций в очереди: 0" in healthcheck.body.decode()
    assert fake.calls == []
    fields = {
        "csrf_token": "csrf",
        "operation": "visit_calendar",
        "action_id": "publish",
        "code": visit.public_code,
    }
    body = urlencode(fields).encode()
    assert (
        app.handle(
            "POST", path, {**headers, "Origin": "https://evil.test"}, body
        ).status
        == 403
    )
    assert (
        app.handle(
            "POST", path, headers, urlencode({**fields, "csrf_token": "wrong"}).encode()
        ).status
        == 403
    )
    assert fake.calls == []
    assert app.handle("POST", path, headers, body).status == 200
    assert len(fake.calls) == 1
    other = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Synthetic other"))
    assert app.handle("POST", f"/profiles/{other}/medical", headers, body).status == 400
    assert len(fake.calls) == 1
    assert "подтверждено" in app.handle("GET", path, headers, b"").body.decode()


def test_panel_note_is_committed_before_gateway(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    service.publish(OWNER, visit.public_code)
    adapter = DatabaseWorkflowAdapter(lambda: session_scope(clean_database), service)

    def callback(event):
        with session_scope(clean_database) as session:
            assert (
                VisitRepository(session).notes(OWNER, visit.public_code)[0].text
                == "Committed"
            )
        raise TimeoutError("private")

    fake.callback = callback
    message = adapter.action(
        OWNER,
        {
            "operation": "visit_question",
            "action_id": "q",
            "code": visit.public_code,
            "text": "Committed",
        },
    )
    assert "очереди" in message


def test_missing_oauth_is_queued_without_gateway(clean_database, tmp_path):
    visit = create_visit(clean_database)
    settings = Settings(google_calendar_root=tmp_path / "calendar")
    service = build_publication_service(settings, clean_database)
    service.calendar.gateway_factory = lambda _: pytest.fail("Unexpected gateway")
    assert (
        service.publish(OWNER, visit.public_code).safe_error == "authorization_missing"
    )
    assert service.snapshot(OWNER, visit.public_code).status == "queued"


def test_local_status_does_not_create_directories(tmp_path):
    root = tmp_path / "absent"
    service = build_calendar_service(Settings(google_calendar_root=root))
    assert service.oauth.local_status(OWNER) == "missing"
    with pytest.raises(FileNotFoundError):
        service.profiles.load(OWNER)
    assert not root.exists()


def test_cli_profile_guard_precedes_file_writes(
    clean_database, disposable_postgres, tmp_path, monkeypatch
):
    settings = disposable_postgres.settings.model_copy(
        update={"google_calendar_root": tmp_path / "calendar"}
    )
    monkeypatch.setattr(composition, "Settings", lambda: settings)
    runner = CliRunner()
    missing = runner.invoke(
        cli.app, ["calendar", "configure", "--profile-id", str(uuid4())]
    )
    assert missing.exit_code == 1
    assert not settings.google_calendar_root.exists()
    assert (
        runner.invoke(
            cli.app, ["calendar", "configure", "--profile-id", str(OWNER)]
        ).exit_code
        == 0
    )
    visit = create_visit(clean_database)
    result = runner.invoke(
        cli.app, ["visit", "calendar", "--profile-id", str(OWNER), visit.public_code]
    )
    assert result.exit_code == 0 and "status=queued" in result.output
    result = runner.invoke(cli.app, ["calendar", "sync", "--profile-id", str(OWNER)])
    assert result.exit_code == 0 and "status=deferred" in result.output


def test_automation_discovers_opt_ins_only_and_defers_missing_auth(
    clean_database, disposable_postgres, tmp_path
):
    settings = disposable_postgres.settings.model_copy(
        update={"google_calendar_root": tmp_path / "calendar"}
    )
    visit = create_visit(clean_database)
    assert tuple(CalendarJobAdapter().discover(settings)) == ()
    service = build_publication_service(settings, clean_database)
    service.publish(OWNER, visit.public_code)
    jobs = tuple(CalendarJobAdapter().discover(settings))
    assert len(jobs) == 1
    assert jobs[0].arguments == (
        "calendar",
        "sync",
        "--profile-id",
        str(OWNER),
        "--limit",
        "100",
    )
    assert jobs[0].not_ready_code == "oauth_not_ready"


def test_composite_foreign_key_rejects_other_profile(clean_database):
    visit = create_visit(clean_database)
    with pytest.raises(IntegrityError), session_scope(clean_database) as session:
        session.add(
            VisitCalendarPublication(
                visit_id=visit.id, profile_id=uuid4(), status="queued"
            )
        )


def test_staging_isolates_all_settings_paths_and_inline_secrets(monkeypatch):
    staging = StagingEnvironment.load(Path.cwd(), Path(".env.staging.example"))
    monkeypatch.setenv("OPENAI_API_KEY", "production-secret")
    monkeypatch.setenv("YANDEX_API_KEY", "production-secret")
    for field in Settings.model_fields.values():
        if isinstance(field.default, Path):
            key = field.validation_alias
            assert key in staging.values
            assert staging.resolve_path(staging.values[key]).is_relative_to(
                staging.staging_root
            )
    env = staging.subprocess_environment()
    assert "OPENAI_API_KEY" not in env and "YANDEX_API_KEY" not in env
    poisoned = replace(
        staging,
        values={**staging.values, "GOOGLE_CALENDAR_ROOT": "data/google-calendar"},
    )
    with pytest.raises(StagingConfigurationError):
        poisoned.validate()


@pytest.mark.parametrize("entrypoint", ["telegram", "panel", "cli"])
@pytest.mark.parametrize("missing_auth", [False, True])
def test_preparation_syncs_committed_questions_from_every_entrypoint(
    clean_database,
    disposable_postgres,
    tmp_path,
    monkeypatch,
    entrypoint,
    missing_auth,
):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    assert service.publish(OWNER, visit.public_code).status == "published"
    assert len(fake.calls) == 1

    def callback(event):
        with session_scope(clean_database) as session:
            notes = VisitRepository(session).notes(OWNER, visit.public_code)
            assert {note.text for note in notes} == set(GENERAL_QUESTIONS)
        assert set(event.questions) == set(GENERAL_QUESTIONS)
        return CalendarResult(
            "stable",
            "deferred" if missing_auth else "updated",
            safe_error="authorization_missing" if missing_auth else None,
        )

    fake.callback = callback
    if entrypoint == "telegram":
        commands = DatabaseVisitCommands(clean_database, service)
        output = commands.handle(context(), f"/visit_prepare {visit.public_code}")
        commands.handle(context(2), f"/visit {visit.public_code}")
    elif entrypoint == "panel":
        adapter = DatabaseWorkflowAdapter(
            lambda: session_scope(clean_database), service
        )
        output = adapter.action(
            OWNER,
            {
                "operation": "visit_prepare",
                "action_id": "prepare",
                "code": visit.public_code,
            },
        )
        adapter.snapshot(OWNER)  # GET's read path must not retry queued publication.
    else:
        monkeypatch.setattr(visit_cli, "Settings", lambda: disposable_postgres.settings)
        monkeypatch.setattr(
            composition, "build_publication_service", lambda _settings, _engine: service
        )
        result = CliRunner().invoke(
            visit_cli.app, ["prepare", "--profile-id", str(OWNER), visit.public_code]
        )
        assert result.exit_code == 0
        output = result.output
        shown = CliRunner().invoke(
            visit_cli.app, ["show", "--profile-id", str(OWNER), visit.public_code]
        )
        assert shown.exit_code == 0
    assert len(fake.calls) == 2
    with session_scope(clean_database) as session:
        assert len(VisitRepository(session).notes(OWNER, visit.public_code)) == 5
    assert service.snapshot(OWNER, visit.public_code).status == (
        "queued" if missing_auth else "published"
    )
    assert ("очереди" if missing_auth else "подтверждены") in output
