"""Explicit-profile visit commands, ready for registration on the root CLI."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Annotated
from uuid import UUID, uuid4

import typer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.visits.preparation import prepare_visit
from health_agent.visits.repository import VisitNotFound, VisitRepository
from health_agent.visits.telegram import (
    parse_visit_time,
    render_brief,
    render_visit,
    summary,
)

app = typer.Typer(
    help="Визиты к врачу, вопросы и записанные ответы.", no_args_is_help=True
)
ProfileOption = Annotated[UUID, typer.Option("--profile-id", help="ID вашего профиля")]


@contextmanager
def _session() -> Iterator[Session]:
    engine = None
    try:
        engine = build_engine(Settings())
        with session_scope(engine) as session:
            yield session
    except (ValueError, RuntimeError, OSError, SQLAlchemyError, VisitNotFound):
        # Neither setup failures nor persistence errors may echo SQL/credentials.
        typer.echo(
            "visit_error: проверьте профиль, код, параметры и доступность БД.", err=True
        )
        raise typer.Exit(code=1) from None
    finally:
        if engine is not None:
            engine.dispose()


@app.command("create")
def create(
    profile_id: ProfileOption,
    title: Annotated[str, typer.Option()],
    when: Annotated[str, typer.Option()],
    timezone: Annotated[str, typer.Option()] = "Europe/Moscow",
    duration_minutes: Annotated[int, typer.Option(min=1, max=1440)] = 60,
    creation_key: Annotated[str | None, typer.Option()] = None,
    source_document_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    with _session() as session:
        start = parse_visit_time(when, timezone)
        visit = VisitRepository(session).create(
            profile_id,
            title=title,
            starts_at=start,
            ends_at=start + timedelta(minutes=duration_minutes),
            timezone_name=timezone,
            creation_key=creation_key or f"cli:{uuid4()}",
            source_document_id=source_document_id,
        )
    typer.echo(f"code={visit.public_code} status={visit.status}")


@app.command("list")
def list_visits(
    profile_id: ProfileOption, limit: Annotated[int, typer.Option(min=1, max=100)] = 20
) -> None:
    with _session() as session:
        visits = VisitRepository(session).list(profile_id, limit=limit)
    typer.echo(f"visits={len(visits)}")
    for visit in visits:
        typer.echo(summary(visit))


@app.command("show")
def show(profile_id: ProfileOption, code: str) -> None:
    with _session() as session:
        repo = VisitRepository(session)
        output = render_visit(repo.get(profile_id, code), repo.notes(profile_id, code))
    typer.echo(output)


@app.command("prepare")
def prepare(profile_id: ProfileOption, code: str) -> None:
    with _session() as session:
        output = render_brief(prepare_visit(session, profile_id, code))
    typer.echo(output)


@app.command("note")
def note(
    profile_id: ProfileOption,
    code: str,
    kind: Annotated[str, typer.Option()],
    text: Annotated[str, typer.Option()],
    action_key: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _session() as session:
        VisitRepository(session).add_note(
            profile_id,
            code,
            kind=kind,
            text=text,
            action_key=action_key or f"cli:{uuid4()}",
        )
    typer.echo("notes_added_or_replayed=1")


@app.command("complete")
def complete(profile_id: ProfileOption, code: str) -> None:
    with _session() as session:
        visit = VisitRepository(session).complete(profile_id, code)
    typer.echo(f"code={visit.public_code} status={visit.status}")


@app.command("cancel")
def cancel(profile_id: ProfileOption, code: str) -> None:
    with _session() as session:
        visit = VisitRepository(session).cancel(profile_id, code)
    typer.echo(f"code={visit.public_code} status={visit.status}")
