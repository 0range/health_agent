from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy import select

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.gmail.api import GoogleGmailGateway
from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.oauth import GmailOAuth
from health_agent.gmail.service import GmailAccountMismatch, GmailService
from health_agent.gmail.stores import (
    LocalGmailProfileStore,
    LocalGmailStateStore,
    LocalGmailTokenStore,
)
from health_agent.gmail.vault_importer import VaultAttachmentImporter
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

app = typer.Typer(help="Personal Health Agent")
review_app = typer.Typer(help="Review imported laboratory candidates.")
dashboard_app = typer.Typer(help="Manage the local Metabase dashboards.")
gmail_app = typer.Typer(help="Manage read-only Gmail medical ingestion.")
app.add_typer(review_app, name="review")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(gmail_app, name="gmail")


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


def _gmail_stores(
    settings: Settings,
) -> tuple[LocalGmailProfileStore, LocalGmailTokenStore, LocalGmailStateStore]:
    return (
        LocalGmailProfileStore(settings.gmail_root),
        LocalGmailTokenStore(settings.gmail_root),
        LocalGmailStateStore(settings.gmail_root),
    )


@gmail_app.command("configure")
def configure_gmail(
    profile_id: UUID,
    account_id: str,
    lookback_days: int = 7,
    trusted_sender: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Add or update one Gmail account slot for a health profile."""
    settings = Settings()
    profiles, _, _ = _gmail_stores(settings)
    profile = (
        profiles.load(str(profile_id))
        if profiles.exists(str(profile_id))
        else GmailProfile.empty(profile_id)
    )
    try:
        current = profile.account(account_id)
    except KeyError:
        current = None
    account = GmailAccount.create(
        account_id,
        initial_lookback_days=lookback_days,
        trusted_senders=(
            trusted_sender
            if trusted_sender is not None
            else (() if current is None else current.trusted_senders)
        ),
    )
    if current is not None and current.email is not None:
        account = account.with_email(current.email)
    profiles.save(profile.upsert_account(account))
    typer.echo(
        f"status=configured profile={profile.profile_id} account={account.account_id} "
        f"lookback_days={account.initial_lookback_days}"
    )


@gmail_app.command("auth")
def authorize_gmail(profile_id: UUID, account_id: str) -> None:
    """Authorize one Gmail account through a local Desktop OAuth callback."""
    settings = Settings()
    profiles, tokens, _ = _gmail_stores(settings)
    profile = profiles.load(str(profile_id))
    account = profile.account(account_id)
    credentials = GmailOAuth(settings.google_oauth_client_secrets, tokens).authorize(
        profile.profile_id, account.account_id, force=True
    )
    mailbox = GoogleGmailGateway.from_credentials(credentials).get_profile()
    if account.email is not None and account.email != mailbox.email:
        tokens.clear(profile.profile_id, account.account_id)
        raise GmailAccountMismatch(
            f"Gmail account slot {account.account_id!r} is already bound"
        )
    profiles.save(profile.upsert_account(account.with_email(mailbox.email)))
    typer.echo(
        f"status=authorized profile={profile.profile_id} "
        f"account={account.account_id} email={mailbox.email}"
    )


@gmail_app.command("status")
def gmail_status(profile_id: UUID, account_id: str | None = None) -> None:
    """Show safe local Gmail connection and cursor status."""
    settings = Settings()
    profiles, tokens, state = _gmail_stores(settings)
    profile = profiles.load(str(profile_id))
    accounts = _selected_gmail_accounts(profile, account_id)
    for account in accounts:
        messages, imported, ambiguous = state.counts(
            profile.profile_id, account.account_id
        )
        typer.echo(
            " ".join(
                (
                    "status=configured",
                    f"profile={profile.profile_id}",
                    f"account={account.account_id}",
                    f"authorized={'yes' if tokens.exists(profile.profile_id, account.account_id) else 'no'}",
                    f"email={account.email or 'unknown'}",
                    f"cursor={'ready' if state.get_cursor(profile.profile_id, account.account_id) else 'none'}",
                    f"messages={messages}",
                    f"imported={imported}",
                    f"ambiguous={ambiguous}",
                )
            )
        )


@gmail_app.command("sync")
def sync_gmail(
    profile_id: UUID, account_id: str | None = None, full: bool = False
) -> None:
    """Synchronize one or all configured Gmail accounts for a profile."""
    settings = Settings()
    profiles, tokens, state = _gmail_stores(settings)
    profile = profiles.load(str(profile_id))
    accounts = _selected_gmail_accounts(profile, account_id)
    failed = False
    for account in accounts:
        if not tokens.exists(profile.profile_id, account.account_id):
            failed = True
            typer.echo(
                f"status=failed profile={profile.profile_id} account={account.account_id} "
                "safe_error=oauth_required"
            )
            continue
        try:
            credentials = GmailOAuth(
                settings.google_oauth_client_secrets, tokens
            ).authorize(profile.profile_id, account.account_id)
            report = GmailService(
                profile.profile_id,
                account,
                GoogleGmailGateway.from_credentials(credentials),
                state,
                VaultAttachmentImporter(
                    profile.profile_id,
                    account.account_id,
                    settings.vault_root,
                    settings.temporary_root,
                ),
            ).sync(full=full)
        except Exception as error:  # noqa: BLE001 - isolate configured accounts
            failed = True
            typer.echo(
                f"status=failed profile={profile.profile_id} account={account.account_id} "
                f"safe_error={type(error).__name__}"
            )
            continue
        typer.echo(
            " ".join(
                (
                    "status=synced",
                    f"profile={report.profile_id}",
                    f"account={report.account_id}",
                    f"mode={report.mode}",
                    f"messages={report.messages_seen}",
                    f"imported={report.attachments_imported}",
                    f"ambiguous={report.ambiguous}",
                    f"ignored={report.ignored}",
                    f"unchanged={report.unchanged}",
                    f"removed={report.removed}",
                )
            )
        )
    if failed:
        raise typer.Exit(code=1)


def _selected_gmail_accounts(
    profile: GmailProfile, account_id: str | None
) -> tuple[GmailAccount, ...]:
    return profile.accounts if account_id is None else (profile.account(account_id),)


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
