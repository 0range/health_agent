"""Local construction and explicit CLI entrypoints for visit Calendar publication."""

from typing import Annotated
from uuid import UUID

import typer

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.google_calendar.api import GoogleCalendarGateway
from health_agent.google_calendar.cli import create_cli
from health_agent.google_calendar.oauth import CalendarOAuth
from health_agent.google_calendar.publication import (
    CalendarPublicationService,
)
from health_agent.google_calendar.service import CalendarService
from health_agent.google_calendar.stores import CalendarProfileStore, CalendarTokenStore
from health_agent.models import Profile
from health_agent.panel.models import ConnectorCard


def build_calendar_service(settings: Settings) -> CalendarService:
    profiles = CalendarProfileStore(settings.google_calendar_root / "profiles")
    tokens = CalendarTokenStore(settings.google_calendar_root / "tokens")
    oauth = CalendarOAuth(
        settings.google_calendar_client_secrets, profiles, tokens, GoogleCalendarGateway
    )
    return CalendarService(profiles, tokens, oauth, GoogleCalendarGateway)


def build_publication_service(settings: Settings, engine) -> CalendarPublicationService:
    return CalendarPublicationService(
        engine,
        build_calendar_service(settings),
        settings.google_calendar_root / "locks",
    )


def validate_profile(settings: Settings, profile_id: UUID) -> None:
    engine = build_engine(settings)
    try:
        with session_scope(engine) as session:
            if session.get(Profile, profile_id) is None:
                raise ValueError("unknown_profile")
    finally:
        engine.dispose()


def create_calendar_cli():
    app = create_cli(
        lambda: build_calendar_service(Settings()),
        profile_validator=lambda profile_id: validate_profile(Settings(), profile_id),
    )

    @app.command("sync")
    def sync(
        profile_id: Annotated[UUID, typer.Option("--profile-id")],
        limit: Annotated[int, typer.Option(min=1, max=100)] = 100,
    ):
        engine = None
        try:
            settings = Settings()
            validate_profile(settings, profile_id)
            engine = build_engine(settings)
            results = build_publication_service(settings, engine).sync_profile(
                profile_id, limit
            )
            queued = sum(r.status == "queued" for r in results)
        except Exception:  # noqa: BLE001 - closed CLI error, no SQL/credentials.
            typer.echo("status=failed safe_error=calendar_sync_failed", err=True)
            raise typer.Exit(1) from None
        finally:
            if engine is not None:
                engine.dispose()
        typer.echo(
            f"status={'deferred' if queued else 'succeeded'} visits={len(results)} queued={queued}"
        )

    return app


def publish_cli(profile_id: UUID, code: str) -> None:
    engine = None
    try:
        settings = Settings()
        validate_profile(settings, profile_id)
        engine = build_engine(settings)
        result = build_publication_service(settings, engine).publish(profile_id, code)
    except Exception:  # noqa: BLE001
        typer.echo("status=failed safe_error=calendar_publication_failed", err=True)
        raise typer.Exit(1) from None
    finally:
        if engine is not None:
            engine.dispose()
    typer.echo(f"status={result.status} safe_error={result.safe_error or 'none'}")


class CalendarStatusReader:
    connector = "calendar"

    def __init__(self, publication: CalendarPublicationService):
        self.publication = publication

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        try:
            profile = self.publication.calendar.profiles.load(profile_id)
            authorization = self.publication.calendar.oauth.local_status(profile_id)
            state = (
                "ready"
                if profile.enabled
                and profile.account_subject
                and profile.account_email
                and authorization == "ready"
                else "oauth_required"
            )
        except FileNotFoundError:
            state = "not_configured"
        count = self.publication.backlog(profile_id)
        return (
            ConnectorCard(
                "calendar",
                state,
                f"Calendar: {state}. Публикаций в очереди: {count}. Публикация только по выбору пользователя.",
                last_success_at=self.publication.last_success_at(profile_id),
                error_code="calendar_publications_queued" if count else None,
            ),
        )
