from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.telegram.types import MessageContext
from health_agent.visits.cli import app
from health_agent.visits.repository import VisitRepository
from health_agent.visits.telegram import DatabaseVisitCommands, parse_visit_time


def context(update=1):
    return MessageContext(
        bot_id=42,
        profile_id=DEFAULT_PROFILE_ID,
        telegram_user_id=100,
        chat_id=100,
        message_id=update,
        update_id=update,
        sent_at=None,
        received_at=datetime(2026, 9, 6, tzinfo=UTC),
    )


def test_telegram_complete_workflow_is_durable_and_idempotent(clean_database):
    commands = DatabaseVisitCommands(clean_database)
    new = "/visit_new 2026-10-05T10:00 | Учебный визит"
    response = commands.handle(context(), new)
    assert "Europe/Moscow" in response
    assert commands.handle(context(), new) == response
    with session_scope(clean_database) as session:
        visits = VisitRepository(session).list(DEFAULT_PROFILE_ID)
        assert len(visits) == 1
        code = visits[0].public_code
        assert visits[0].starts_at.hour == 7
    assert "Непроверенных" in commands.handle(context(2), f"/visit_prepare {code}")
    for update, command in [
        (3, f"/visit_question {code} Учебный вопрос"),
        (4, f"/visit_answer {code} Учебный ответ"),
    ]:
        first = commands.handle(context(update), command)
        assert commands.handle(context(update), command) == first
    moved = commands.handle(context(5), f"/visit_move {code} 2026-10-06T11:00")
    assert "2026-10-06 11:00" in moved
    assert "завершён" in commands.handle(context(6), f"/visit_done {code}")
    fresh = DatabaseVisitCommands(clean_database)
    detail = fresh.handle(context(7), f"/visit {code}")
    assert (
        "Вопрос: Учебный вопрос" in detail
        and "Записанный ответ: Учебный ответ" in detail
    )
    assert "/visit_done" not in detail
    with session_scope(clean_database) as session:
        assert len(VisitRepository(session).notes(DEFAULT_PROFILE_ID, code)) == 7
    assert (
        fresh.handle(replace(context(8), profile_id=uuid4()), f"/visit {code}")
        == fresh.unavailable_text
    )
    assert fresh.handle(context(9), "/unknown") is None
    assert fresh.handle(context(10), "/visit_new invalid") == fresh.usage_text
    assert fresh.handle(context(11), "/visits extra") == fresh.usage_text


def test_telegram_long_detail_bounded_and_cancelled_is_readable(clean_database):
    commands = DatabaseVisitCommands(clean_database)
    commands.handle(context(), "/visit_new 2026-10-05T10:00 | Визит")
    with session_scope(clean_database) as session:
        repo = VisitRepository(session)
        code = repo.list(DEFAULT_PROFILE_ID)[0].public_code
        for i in range(3):
            repo.add_note(
                DEFAULT_PROFILE_ID,
                code,
                kind="answer",
                text="я" * 10000,
                action_key=f"long:{i}",
            )
    result = commands.handle(context(2), f"/visit {code}")
    assert len(result) <= 12000 and "достигнут лимит" in result
    assert "отменён" in commands.handle(context(3), f"/visit_cancel {code}")
    assert "/visit_question" not in commands.handle(context(4), f"/visit {code}")
    assert (
        commands.handle(context(5), f"/visit_answer {code} late") == commands.usage_text
    )


@pytest.mark.parametrize(
    "value,zone",
    [
        ("2026-03-29T02:30", "Europe/Berlin"),
        ("2026-10-25T02:30", "Europe/Berlin"),
        ("2026-10-25", "Europe/Moscow"),
    ],
)
def test_explicit_local_time_rejects_ambiguous_nonexistent_or_missing_time(value, zone):
    with pytest.raises(ValueError):
        parse_visit_time(value, zone)


def test_cli_lifecycle_uses_explicit_profile_and_safe_errors(
    clean_database, monkeypatch
):
    monkeypatch.setattr(
        "health_agent.visits.cli.Settings",
        lambda: Settings(
            database_url=clean_database.url.render_as_string(hide_password=False)
        ),
    )
    runner = CliRunner()
    profile = ["--profile-id", str(DEFAULT_PROFILE_ID)]
    args = [
        "create",
        *profile,
        "--title",
        "Fixture",
        "--when",
        "2026-10-05T10:00",
        "--creation-key",
        "cli-fixture",
    ]
    created = runner.invoke(app, args)
    assert created.exit_code == 0, created.output
    code = created.stdout.split("code=", 1)[1].split()[0]
    assert runner.invoke(app, args).stdout == created.stdout
    for cmd in [
        ["list", *profile],
        ["show", code, *profile],
        ["prepare", code, *profile],
        [
            "note",
            code,
            *profile,
            "--kind",
            "answer",
            "--text",
            "Учебный ответ",
            "--action-key",
            "cli-note",
        ],
        ["complete", code, *profile],
    ]:
        result = runner.invoke(app, cmd)
        assert result.exit_code == 0, result.output
    assert "Учебный ответ" in runner.invoke(app, ["show", code, *profile]).stdout
    assert "status=completed" in runner.invoke(app, args).stdout
    for command in ["list", "show", "prepare", "note", "complete", "cancel"]:
        assert runner.invoke(app, [command]).exit_code != 0
    foreign = runner.invoke(app, ["show", code, "--profile-id", str(uuid4())])
    assert foreign.exit_code == 1 and "Fixture" not in foreign.output

    def broken():
        raise RuntimeError("password=PRIVATE_TOKEN SELECT * FROM visits")

    monkeypatch.setattr("health_agent.visits.cli.Settings", broken)
    failed = runner.invoke(app, ["list", *profile])
    assert (
        failed.exit_code == 1
        and "PRIVATE_TOKEN" not in failed.output
        and "SELECT" not in failed.output
    )
