"""Composable Calendar configuration CLI; publishing remains visit-owned."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer

from health_agent.google_calendar.models import CalendarProfile

ProfileOption = Annotated[UUID, typer.Option("--profile-id")]


def create_cli(service_factory, *, profile_validator=lambda _profile_id: None):
    app = typer.Typer(help="Google Calendar visit adapter")

    @app.command()
    def configure(
        profile_id: ProfileOption, calendar_id: str = "primary", enabled: bool = True
    ):
        try:
            profile_validator(profile_id)
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
        except Exception:  # noqa: BLE001 - emit only a closed safe code.
            typer.echo(
                "status=failed safe_error=calendar_configuration_invalid", err=True
            )
            raise typer.Exit(1) from None
        typer.echo("status=configured")

    @app.command()
    def status(profile_id: ProfileOption):
        try:
            profile_validator(profile_id)
            service = service_factory()
            profile = service.profiles.load(profile_id)
            authorization = service.oauth.local_status(profile_id)
        except Exception:  # noqa: BLE001
            typer.echo("status=failed safe_error=calendar_status_unavailable", err=True)
            raise typer.Exit(1) from None
        typer.echo(
            f"enabled={str(profile.enabled).lower()} authorization={authorization}"
        )

    @app.command()
    def authorize(
        profile_id: ProfileOption, interactive: bool = False, force: bool = False
    ):
        try:
            profile_validator(profile_id)
            service_factory().oauth.authorize(
                profile_id, interactive=interactive, force=force
            )
        except Exception:  # noqa: BLE001
            typer.echo(
                "status=failed safe_error=calendar_authorization_failed", err=True
            )
            raise typer.Exit(1) from None
        typer.echo("status=authorized")

    return app
