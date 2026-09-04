from datetime import date
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
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentSourceRecord,
    LabObservation,
    ReviewStatus,
    SourceRecord,
)
from health_agent.vault import FileVault
from health_agent.whoop.auth_service import (
    complete_whoop_authorization,
    open_and_wait_for_whoop_authorization,
)
from health_agent.whoop.client import WhoopClient
from health_agent.whoop.oauth import WhoopOAuth
from health_agent.whoop.repository import register_authorized_connection
from health_agent.whoop.status import get_whoop_status
from health_agent.whoop.sync import sync_whoop
from health_agent.whoop.tokens import TokenStore

app = typer.Typer(help="Personal Health Agent")
review_app = typer.Typer(help="Review imported laboratory candidates.")
dashboard_app = typer.Typer(help="Manage the local Metabase dashboards.")
whoop_app = typer.Typer(help="Connect and synchronize WHOOP accounts.")
app.add_typer(review_app, name="review")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(whoop_app, name="whoop")


@app.callback()
def health_agent() -> None:
    """Personal Health Agent."""


@app.command("import-file")
def import_file(
    path: Path,
    source_uri: str | None = None,
    collected_date: str | None = None,
    issued_date: str | None = None,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> None:
    """Store and extract one local PDF."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        report = import_document(
            session,
            FileVault(settings.vault_root),
            path,
            source_uri,
            profile_id=profile_id,
            collected_date=_medical_date("collected-date", collected_date),
            issued_date=_medical_date("issued-date", issued_date),
        )
    typer.echo(
        " ".join(
            (
                f"status={report.status}",
                f"document_id={report.document_id}",
                f"processing_status={report.processing_status}",
                f"candidates={report.candidate_count}",
                f"review_items={report.review_count}",
            )
        )
    )


@review_app.command("list")
def list_review_items(profile_id: UUID = DEFAULT_PROFILE_ID) -> None:
    """List candidate source evidence awaiting a human decision."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        filename = (
            select(SourceRecord.external_id)
            .join(
                DocumentSourceRecord,
                DocumentSourceRecord.source_record_id == SourceRecord.id,
            )
            .where(DocumentSourceRecord.document_id == LabObservation.document_id)
            .order_by(SourceRecord.received_at, SourceRecord.id)
            .limit(1)
            .scalar_subquery()
        )
        rows = session.execute(
            select(LabObservation, filename)
            .join(LabObservation.document)
            .where(LabObservation.status == ReviewStatus.NEEDS_REVIEW)
            .where(Document.profile_id == profile_id)
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
def approve_review_item(
    observation_id: UUID, profile_id: UUID = DEFAULT_PROFILE_ID
) -> None:
    """Approve one pending observation by UUID."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        approve_observation(session, observation_id, profile_id=profile_id)
    typer.echo(f"status=approved observation_id={observation_id}")


@review_app.command("reject")
def reject_review_item(
    observation_id: UUID, profile_id: UUID = DEFAULT_PROFILE_ID
) -> None:
    """Reject one pending observation by UUID."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        reject_observation(session, observation_id, profile_id=profile_id)
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


@whoop_app.command("auth")
def whoop_auth(
    profile_id: UUID = DEFAULT_PROFILE_ID,
    account: str = "main",
) -> None:
    """Authorize one WHOOP account for one local person profile."""
    settings = Settings()
    oauth = _whoop_oauth(settings)
    pending, query = open_and_wait_for_whoop_authorization(oauth)
    authorized = complete_whoop_authorization(
        oauth,
        TokenStore(settings.whoop_token_root),
        str(profile_id),
        account,
        pending,
        query,
    )
    with session_scope(build_engine(settings)) as session:
        register_authorized_connection(
            session,
            profile_id,
            account,
            authorized.external_user_id,
            authorized.granted_scopes,
        )
    typer.echo(f"status=connected profile_id={profile_id} account={account}")


@whoop_app.command("status")
def whoop_status(
    profile_id: UUID = DEFAULT_PROFILE_ID,
    account: str = "main",
) -> None:
    """Show freshness and safe record counts without personal details."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        status = get_whoop_status(session, profile_id, account)
    last_success = (
        status.last_success_at.isoformat() if status.last_success_at else "never"
    )
    typer.echo(
        " ".join(
            (
                f"configured={str(status.configured).lower()}",
                f"auth={status.auth_status}",
                f"last_success={last_success}",
                f"error={status.last_error_code or 'none'}",
                f"weight_available={str(status.weight_available).lower()}",
                f"cycles={status.cycle_count}",
                f"recoveries={status.recovery_count}",
                f"sleeps={status.sleep_count}",
                f"workouts={status.workout_count}",
            )
        )
    )


@whoop_app.command("sync")
def whoop_sync(
    profile_id: UUID = DEFAULT_PROFILE_ID,
    account: str = "main",
    full: bool = typer.Option(
        False, "--full", help="Fetch all history available from WHOOP."
    ),
) -> None:
    """Backfill or incrementally synchronize one WHOOP account."""
    settings = Settings()
    oauth = _whoop_oauth(settings)
    client = WhoopClient(
        oauth,
        TokenStore(settings.whoop_token_root),
        str(profile_id),
        account,
    )
    with session_scope(build_engine(settings)) as session:
        report = sync_whoop(session, profile_id, account, client, full=full)
    typer.echo(
        " ".join(
            (
                f"status={report.status}",
                f"mode={report.mode}",
                f"raw_created={report.raw_created}",
                f"created={report.normalized_created}",
                f"updated={report.normalized_updated}",
                f"unchanged={report.unchanged}",
                f"error={report.safe_error_code or 'none'}",
            )
        )
    )
    if report.status != "succeeded":
        raise typer.Exit(code=1)


def main() -> None:
    app()


def _medical_date(option_name: str, value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter(
            "must use YYYY-MM-DD", param_hint=f"--{option_name}"
        ) from error


def _whoop_oauth(settings: Settings) -> WhoopOAuth:
    if settings.whoop_client_id is None or settings.whoop_client_secret is None:
        raise typer.BadParameter(
            "set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET before connecting WHOOP"
        )
    return WhoopOAuth(
        settings.whoop_client_id,
        settings.whoop_client_secret.get_secret_value(),
        settings.whoop_redirect_uri,
    )
