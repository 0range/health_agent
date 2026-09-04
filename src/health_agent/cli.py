from pathlib import Path
from uuid import UUID

import typer
from sqlalchemy import select

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.importer import (
    approve_observation,
    import_document,
    reject_observation,
)
from health_agent.metabase import bootstrap_metabase
from health_agent.models import Document, LabObservation, ReviewStatus, SourceRecord
from health_agent.vault import FileVault

app = typer.Typer(help="Personal Health Agent")
review_app = typer.Typer(help="Review imported laboratory candidates.")
dashboard_app = typer.Typer(help="Manage the local Metabase dashboards.")
app.add_typer(review_app, name="review")
app.add_typer(dashboard_app, name="dashboard")


@app.callback()
def health_agent() -> None:
    """Personal Health Agent."""


@app.command("import-file")
def import_file(path: Path, source_uri: str | None = None) -> None:
    """Store and extract one local PDF."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        report = import_document(
            session, FileVault(settings.vault_root), path, source_uri
        )
    typer.echo(
        " ".join(
            (
                f"status={report.status}",
                f"document_id={report.document_id}",
                f"candidates={report.candidate_count}",
                f"review_items={report.review_count}",
            )
        )
    )


@review_app.command("list")
def list_review_items() -> None:
    """List candidate source evidence awaiting a human decision."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        rows = session.execute(
            select(LabObservation, SourceRecord.external_id)
            .join(LabObservation.document)
            .join(Document.source_record)
            .where(LabObservation.status == ReviewStatus.NEEDS_REVIEW)
            .order_by(LabObservation.created_at, LabObservation.id)
        ).all()
    for observation, filename in rows:
        typer.echo(
            " ".join(
                (
                    f"observation_id={observation.id}",
                    f"source_name={observation.source_name}",
                    f"source_value={observation.source_value}",
                    f"source_unit={observation.source_unit or ''}",
                    f"page={observation.page_number}",
                    f"filename={filename}",
                )
            )
        )


@review_app.command("approve")
def approve_review_item(observation_id: UUID) -> None:
    """Approve one pending observation by UUID."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        approve_observation(session, observation_id)
    typer.echo(f"status=approved observation_id={observation_id}")


@review_app.command("reject")
def reject_review_item(observation_id: UUID) -> None:
    """Reject one pending observation by UUID."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        reject_observation(session, observation_id)
    typer.echo(f"status=rejected observation_id={observation_id}")


@dashboard_app.command("setup")
def setup_dashboard() -> None:
    """Provision the verified laboratory history dashboard."""
    result = bootstrap_metabase(Settings())
    typer.echo(
        " ".join(
            (
                "status=ready",
                f"dashboard_id={result.dashboard_id}",
                f"card_id={result.card_id}",
                f"url={result.dashboard_url}",
                f"admin_email={result.admin_email}",
            )
        )
    )


def main() -> None:
    app()
