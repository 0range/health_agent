"""Content-free commands for opt-in, scheduled work, backfill and recovery."""

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

import typer

from health_agent.config import Settings
from health_agent.db import build_engine
from health_agent.lab_extraction.service import LabExtractionService
from health_agent.lab_extraction.types import ExtractionError

app = typer.Typer(help="Extract imported laboratory pages into explicit review only.")


def build_service() -> LabExtractionService:
    settings = Settings()
    return LabExtractionService(build_engine(settings), settings)


def _call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except ExtractionError as error:
        if error.safe_code == "extraction_busy":
            typer.echo("status=deferred safe_error=extraction_busy")
            raise typer.Exit(0) from None
        typer.echo(f"status=failed safe_error={error.safe_code}")
        raise typer.Exit(1) from None
    except Exception:  # noqa: BLE001 -- no SQL parameters, source text or key paths
        typer.echo("status=failed safe_error=extraction_failed")
        raise typer.Exit(1) from None


@app.command()
def configure(
    profile_id: UUID,
    openai: Annotated[
        bool,
        typer.Option(
            "--openai/--no-openai",
            help="Explicit permission to send bounded page text to OpenAI.",
        ),
    ] = False,
    disabled: bool = False,
    daily_budget: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """Enable local processing; cloud is disabled unless explicitly requested."""
    _call(
        lambda: build_service().configure(
            profile_id, enabled=not disabled, openai=openai, daily_budget=daily_budget
        )
    )
    typer.echo(
        f"status=succeeded profile={profile_id} enabled={str(not disabled).lower()} cloud_enabled={str(openai).lower()} daily_budget={daily_budget}"
    )


@app.command()
def run(
    profile_id: UUID,
    limit: Annotated[int, typer.Option(min=1, max=20)] = 4,
    cloud_limit: Annotated[int, typer.Option(min=0, max=10)] = 2,
) -> None:
    """Discover/backfill new pages and process one bounded batch."""
    report = _call(
        lambda: build_service().run(profile_id, limit=limit, cloud_limit=cloud_limit)
    )
    typer.echo(" ".join(f"{key}={value}" for key, value in asdict(report).items()))


@app.command()
def status(
    profile_id: UUID,
    details: bool = False,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    offset: Annotated[int, typer.Option(min=0, max=1_000_000)] = 0,
) -> None:
    """Show counts and optional bounded recovery IDs/codes, never source text."""
    report = _call(lambda: build_service().status(profile_id))
    typer.echo(
        " ".join(f"{key}={str(value).lower()}" for key, value in asdict(report).items())
    )
    if details:
        rows = _call(
            lambda: build_service().queue.diagnostics(
                profile_id, limit=limit, offset=offset
            )
        )
        for row in rows:
            typer.echo(" ".join(f"{key}={value}" for key, value in asdict(row).items()))


@app.command()
def retry(
    profile_id: UUID, document_id: UUID, acknowledge_unknown: bool = False
) -> None:
    """Requeue attention pages; acknowledge unknown cloud costs explicitly."""
    count = _call(
        lambda: build_service().retry(
            profile_id, document_id, acknowledge_unknown=acknowledge_unknown
        )
    )
    typer.echo(f"status=succeeded requeued={count}")
