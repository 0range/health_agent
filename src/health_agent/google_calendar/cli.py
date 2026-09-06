"""Composable Calendar configuration CLI; publishing remains visit-owned."""

from __future__ import annotations

from uuid import UUID

import typer

from health_agent.google_calendar.models import CalendarProfile


def create_cli(service_factory):
    app = typer.Typer(help="Google Calendar visit adapter")

    @app.command()
    def configure(profile_id: UUID, calendar_id: str = "primary", enabled: bool = True):
        service = service_factory()
        try:
            existing = service.profiles.load(profile_id)
        except FileNotFoundError:
            existing = CalendarProfile(profile_id)
        service.profiles.save(
            CalendarProfile(
                profile_id,
                calendar_id=calendar_id,
                account_subject=existing.account_subject,
                account_email=existing.account_email,
                enabled=enabled,
            )
        )
        typer.echo("status=configured")

    @app.command()
    def status(profile_id: UUID):
        service = service_factory()
        profile = service.profiles.load(profile_id)
        typer.echo(
            f"enabled={str(profile.enabled).lower()} authorization={service.oauth.local_status(profile_id)}"
        )

    @app.command()
    def authorize(profile_id: UUID, interactive: bool = False, force: bool = False):
        service_factory().oauth.authorize(
            profile_id, interactive=interactive, force=force
        )
        typer.echo("status=authorized")

    return app
