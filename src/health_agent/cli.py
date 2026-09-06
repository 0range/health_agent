import os
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from sqlalchemy import select

from health_agent.automation.launchd import (
    LaunchdError,
    LaunchdManager,
    LaunchdPaths,
    rotate_safe_logs,
)
from health_agent.automation.registry import configured_job_adapters
from health_agent.automation.runner import AutomationRunner, SubprocessJobExecutor
from health_agent.automation.storage import (
    AutomationState,
    GlobalRunLock,
    require_private_file,
)
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.gmail.api import GoogleGmailGateway
from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.medical_importer import MedicalAttachmentImporter
from health_agent.gmail.message_inbox import MedicalMessageInbox
from health_agent.gmail.oauth import GmailOAuth, OAuthRequired
from health_agent.gmail.preparation import SafeAttachmentPreparer
from health_agent.gmail.service import GmailAccountMismatch, GmailService
from health_agent.gmail.stores import (
    LocalGmailProfileStore,
    LocalGmailStateStore,
    LocalGmailTokenStore,
)
from health_agent.google_drive.api import GoogleDriveGateway
from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.medical_consumer import MedicalDriveConsumer
from health_agent.google_drive.oauth import DriveOAuth
from health_agent.google_drive.service import DriveProfileMismatch, DriveService
from health_agent.google_drive.stores import (
    LocalProfileStore,
    LocalSyncStateStore,
    LocalTokenStore,
)
from health_agent.google_sheets.api import GoogleSheetsGateway
from health_agent.google_sheets.oauth import SheetsOAuth
from health_agent.google_sheets.service import (
    SheetsService,
    SheetsSyncFailure,
    WorkbookOwnershipError,
)
from health_agent.google_sheets.sources import collect_source_statuses
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsStateStore,
    LocalSheetsTokenStore,
)
from health_agent.importer import (
    approve_observation,
    correct_observation,
    import_document,
    reject_observation,
    set_document_medical_dates,
)
from health_agent.lab_extraction.cli import app as lab_extraction_app
from health_agent.medical_dates import recover_document_dates
from health_agent.metabase import bootstrap_metabase
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentSourceRecord,
    LabObservation,
    Profile,
    ReviewStatus,
    SourceRecord,
)
from health_agent.panel.http import serve_panel
from health_agent.panel.service import build_panel_service
from health_agent.questions.composition import (
    build_question_application,
    build_telegram_question_runtime,
    question_status,
    safe_question_setup_error,
)
from health_agent.questions.models import EvidenceSource
from health_agent.reminders.dispatcher import DispatchReport, ReminderDispatcher
from health_agent.reminders.launchd import (
    REMINDER_LABEL,
    ReminderLaunchdManager,
    ReminderLaunchdPaths,
    rotate_reminder_logs,
)
from health_agent.reminders.repository import ReminderRepository
from health_agent.reminders.telegram import parse_snooze_duration
from health_agent.reminders.time import parse_local_datetime
from health_agent.staging import (
    StagingConfigurationError,
    StagingEnvironment,
    StagingManager,
)
from health_agent.telegram.admin import DatabaseProfileDirectory, TelegramAdminService
from health_agent.telegram.api import TelegramBotAPI
from health_agent.telegram.launchd import (
    TELEGRAM_LABEL,
    TelegramLaunchdError,
    TelegramLaunchdManager,
    TelegramLaunchdPaths,
    TelegramServiceRunner,
)
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.vault import FileVault
from health_agent.visits.cli import app as visit_app
from health_agent.whoop.auth_service import (
    complete_whoop_authorization,
    open_and_wait_for_whoop_authorization,
    publish_whoop_authorization,
    validate_whoop_authorization_target,
)
from health_agent.whoop.client import WhoopClient
from health_agent.whoop.dashboard import bootstrap_whoop_dashboard
from health_agent.whoop.oauth import WhoopOAuth
from health_agent.whoop.status import get_whoop_status
from health_agent.whoop.sync import sync_whoop
from health_agent.whoop.tokens import TokenStore

app = typer.Typer(help="Personal Health Agent")
review_app = typer.Typer(help="Review imported laboratory candidates.")
dashboard_app = typer.Typer(help="Manage the local Metabase dashboards.")
whoop_app = typer.Typer(help="Connect and synchronize WHOOP accounts.")
profile_app = typer.Typer(help="Manage local person profiles.")
gmail_app = typer.Typer(help="Manage read-only Gmail medical ingestion.")
telegram_app = typer.Typer(help="Configure the local Telegram connector.")
panel_app = typer.Typer(help="Serve the local management panel.")
staging_app = typer.Typer(help="Manage the isolated local staging environment.")
drive_app = typer.Typer(help="Manage read-only Google Drive profiles.")
automation_app = typer.Typer(help="Run and manage safe local connector automation.")
question_app = typer.Typer(
    help="Ask profile-scoped questions from verified health data."
)
reminder_app = typer.Typer(help="Manage explicitly confirmed health reminders.")
sheets_app = typer.Typer(help="Publish labs and review decisions in Google Sheets.")
QUESTION_PROFILE_OPTION = typer.Option(..., "--profile-id")
app.add_typer(review_app, name="review")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(whoop_app, name="whoop")
app.add_typer(profile_app, name="profile")
app.add_typer(gmail_app, name="gmail")
app.add_typer(telegram_app, name="telegram")
app.add_typer(panel_app, name="panel")
app.add_typer(staging_app, name="staging")
app.add_typer(drive_app, name="drive")
app.add_typer(automation_app, name="automation")
app.add_typer(question_app, name="question")
app.add_typer(reminder_app, name="reminder")
app.add_typer(sheets_app, name="sheets")
app.add_typer(lab_extraction_app, name="lab-extract")
app.add_typer(visit_app, name="visit")


@app.callback()
def health_agent() -> None:
    """Personal Health Agent."""


@automation_app.command("sync")
def automation_sync(
    env_file: Annotated[Path, typer.Option("--env-file")],
    force_full: Annotated[bool, typer.Option("--full")] = False,
) -> None:
    """Synchronize configured targets without cross-source blocking."""
    try:
        runner, _, _ = _automation_components(env_file)
        results = runner.run(force_full=force_full)
    except (LaunchdError, RuntimeError, ValueError):
        typer.echo("status=failed safe_error=automation_configuration_failed", err=True)
        raise typer.Exit(code=1) from None
    for result in results:
        typer.echo(result.safe_line())
    counts = {
        status: sum(result.status == status for result in results)
        for status in ("succeeded", "deferred", "failed", "timed_out", "skipped")
    }
    typer.echo(
        " ".join(
            (
                f"summary jobs={len(results)}",
                f"succeeded={counts['succeeded']}",
                f"deferred={counts['deferred']}",
                f"failed={counts['failed']}",
                f"timed_out={counts['timed_out']}",
                f"skipped={counts['skipped']}",
            )
        )
    )
    if counts["failed"] or counts["timed_out"]:
        raise typer.Exit(code=1)


@automation_app.command("render")
def automation_render(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    """Render an inspectable LaunchAgent plist without loading it."""
    manager = _automation_manager_or_exit(env_file)
    try:
        path = manager.render()
    except (LaunchdError, RuntimeError, ValueError):
        typer.echo("status=failed safe_error=launchd_operation_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status=rendered label={path.stem}")


@automation_app.command("install")
def automation_install(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    """Install and load the one managed user LaunchAgent."""
    manager = _automation_manager_or_exit(env_file)
    try:
        status = manager.install()
    except (LaunchdError, RuntimeError, ValueError):
        typer.echo("status=failed safe_error=launchd_operation_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={status} label=com.orange.health-agent.sync")


@automation_app.command("status")
def automation_status(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    """Report whether the managed LaunchAgent is loaded."""
    manager = _automation_manager_or_exit(env_file)
    try:
        status = manager.status()
    except (LaunchdError, RuntimeError, ValueError):
        typer.echo("status=failed safe_error=launchd_operation_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={status} label=com.orange.health-agent.sync")


@automation_app.command("stop")
def automation_stop(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    """Stop scheduled synchronization while retaining managed files."""
    manager = _automation_manager_or_exit(env_file)
    try:
        status = manager.stop()
    except (LaunchdError, RuntimeError, ValueError):
        typer.echo("status=failed safe_error=launchd_operation_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={status} label=com.orange.health-agent.sync files=retained")


@automation_app.command("remove")
def automation_remove(env_file: Annotated[Path, typer.Option("--env-file")]) -> None:
    """Stop automation and remove only its two managed plist files."""
    manager = _automation_manager_or_exit(env_file)
    try:
        status = manager.remove()
    except (LaunchdError, RuntimeError, ValueError):
        typer.echo("status=failed safe_error=launchd_operation_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={status} label=com.orange.health-agent.sync")


@panel_app.command("serve")
def serve_management_panel() -> None:
    """Serve the local, loopback-only management panel."""
    settings = Settings()
    service = build_panel_service(settings)
    server = serve_panel(service, host=settings.panel_host, port=settings.panel_port)
    typer.echo(f"http://{settings.panel_host}:{settings.panel_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@profile_app.command("create")
def create_profile(name: str) -> None:
    """Create a separate local person profile and print its UUID."""
    profile_id = uuid4()
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        session.add(Profile(id=profile_id, name=name))
    typer.echo(f"status=created profile_id={profile_id} name={name}")


@profile_app.command("list")
def list_profiles() -> None:
    """List local person profiles without health data."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        profiles = session.execute(
            select(Profile.id, Profile.name).order_by(Profile.created_at)
        ).all()
    for profile_id, name in profiles:
        typer.echo(f"profile_id={profile_id} name={name}")


@app.command("import-file")
def import_file(
    path: Path,
    source_uri: str | None = None,
    collected_date: str | None = None,
    issued_date: str | None = None,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> None:
    """Store and extract one local PDF, JPEG or PNG."""
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
            select(
                LabObservation.id,
                LabObservation.source_name,
                LabObservation.source_value,
                LabObservation.source_unit,
                LabObservation.page_number,
                filename,
                Document.id,
                Document.collected_date,
                Document.issued_date,
            )
            .join(LabObservation.document)
            .where(LabObservation.status == ReviewStatus.NEEDS_REVIEW)
            .where(Document.profile_id == profile_id)
            .order_by(LabObservation.created_at, LabObservation.id)
        ).all()
    for (
        observation_id,
        source_name,
        source_value,
        source_unit,
        page_number,
        filename,
        document_id,
        collected_date,
        issued_date,
    ) in rows:
        typer.echo(
            " ".join(
                (
                    f"observation_id={observation_id}",
                    f"source_name={source_name}",
                    f"source_value={source_value}",
                    f"source_unit={source_unit or ''}",
                    f"page={page_number}",
                    f"filename={filename}",
                    f"document_id={document_id}",
                    f"collected_date={collected_date or ''}",
                    f"issued_date={issued_date or ''}",
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


@review_app.command("correct")
def correct_review_item(
    observation_id: UUID,
    value: Annotated[str, typer.Option("--value")],
    unit: Annotated[str, typer.Option("--unit")],
    profile_id: Annotated[UUID, typer.Option("--profile-id")],
    canonical_name: Annotated[str | None, typer.Option("--canonical-name")] = None,
) -> None:
    """Explicitly version one pending value/unit correction; keep source lineage."""
    try:
        settings = Settings()
        with session_scope(build_engine(settings)) as session:
            corrected = correct_observation(
                session,
                observation_id,
                source_value=value,
                source_unit=unit,
                profile_id=profile_id,
                canonical_name=canonical_name,
            )
            corrected_id = corrected.id
    except Exception:  # noqa: BLE001 -- local DB/source diagnostics are private
        typer.echo(
            "Correction not applied. Check the pending item, value, unit and profile."
        )
        raise typer.Exit(1) from None
    typer.echo(
        f"status=corrected observation_id={observation_id} "
        f"corrected_observation_id={corrected_id}"
    )


@review_app.command("set-date")
def set_review_document_date(
    document_id: UUID,
    collected_date: str | None = None,
    issued_date: str | None = None,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> None:
    """Set a human-reviewed medical date without using an import timestamp."""
    settings = Settings()
    with session_scope(build_engine(settings)) as session:
        document = set_document_medical_dates(
            session,
            document_id,
            collected_date=_medical_date("collected-date", collected_date),
            issued_date=_medical_date("issued-date", issued_date),
            profile_id=profile_id,
        )
        saved_id = document.id
        saved_collected_date = document.collected_date
        saved_issued_date = document.issued_date
    typer.echo(
        f"status=date_set document_id={saved_id} "
        f"collected_date={saved_collected_date or ''} "
        f"issued_date={saved_issued_date or ''}"
    )


@review_app.command("recover-dates")
def recover_review_document_dates(
    profile_id: Annotated[UUID, typer.Option("--profile-id")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 200,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Preview or apply conservative labelled-date recovery."""
    mode = "apply" if apply else "dry_run"
    try:
        settings = Settings()
        with session_scope(build_engine(settings)) as session:
            counts = recover_document_dates(
                session, profile_id=profile_id, limit=limit, apply=apply
            )
    except Exception:  # noqa: BLE001 -- DB and extracted-text diagnostics are private
        typer.echo(
            f"status=failed mode={mode} scanned=0 eligible=0 changed=0 blocked=0 "
            "safe_error_code=medical_date_recovery_failed"
        )
        raise typer.Exit(1) from None
    typer.echo(
        f"status=complete mode={mode} scanned={counts['scanned']} "
        f"eligible={counts['eligible']} changed={counts['changed']} "
        f"blocked={counts['blocked']} safe_error_code="
    )


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


@dashboard_app.command("setup-whoop")
def setup_whoop_dashboard(profile_id: UUID = DEFAULT_PROFILE_ID) -> None:
    """Provision the profile-isolated WHOOP overview dashboard."""
    settings = Settings()
    if not _profile_exists(settings, profile_id):
        raise typer.BadParameter("profile does not exist", param_hint="--profile-id")
    result = bootstrap_whoop_dashboard(settings, profile_id)
    typer.echo(
        " ".join(
            (
                "status=ready",
                f"profile_id={profile_id}",
                f"dashboard_id={result.dashboard_id}",
                f"cards={len(result.card_ids)}",
                f"url={result.dashboard_url}",
            )
        )
    )


@staging_app.command("start")
def staging_start(
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Start isolated staging services and migrate the staging database."""
    manager = _staging_manager(env_file)
    manager.start()
    typer.echo("status=started project=health-agent-staging volumes=preserved")


@staging_app.command("status")
def staging_status(
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Show only the dedicated staging Compose project."""
    manager = _staging_manager(env_file)
    manager.status()


@staging_app.command("stop")
def staging_stop(
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Stop staging while preserving its volumes and local files."""
    manager = _staging_manager(env_file)
    manager.stop()
    typer.echo("status=stopped project=health-agent-staging volumes=preserved")


@staging_app.command("clean")
def staging_clean(
    confirmation: Annotated[str, typer.Option("--confirm")],
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Delete staging Compose volumes after an exact project confirmation."""
    manager = _staging_manager(env_file)
    try:
        manager.clean(confirmation)
    except StagingConfigurationError as error:
        raise typer.BadParameter(str(error), param_hint="--confirm") from error
    typer.echo("status=cleaned project=health-agent-staging local_files=preserved")


@staging_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def staging_run(
    context: typer.Context,
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Run an application command using staging settings; use `run -- ...`."""
    manager = _staging_manager(env_file)
    try:
        manager.run_application(context.args)
    except StagingConfigurationError as error:
        raise typer.BadParameter(str(error)) from error


@whoop_app.command("auth")
def whoop_auth(
    profile_id: UUID = DEFAULT_PROFILE_ID,
    account: str = "main",
) -> None:
    """Authorize one WHOOP account for one local person profile."""
    settings = Settings()
    oauth = _whoop_oauth(settings)
    token_store = TokenStore(settings.whoop_token_root)
    profile_key = str(profile_id)
    token_store.validate_target(profile_key, account)
    engine = build_engine(settings)
    with session_scope(engine) as session:
        validate_whoop_authorization_target(
            session, token_store, profile_id, profile_key, account
        )
    pending, query = open_and_wait_for_whoop_authorization(oauth)
    authorized = complete_whoop_authorization(
        oauth,
        pending,
        query,
    )
    publish_whoop_authorization(
        lambda: session_scope(engine),
        token_store,
        profile_id,
        profile_key,
        account,
        authorized,
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
        status = get_whoop_status(
            session,
            TokenStore(settings.whoop_token_root),
            profile_id,
            str(profile_id),
            account,
        )
    last_success = (
        status.last_success_at.isoformat() if status.last_success_at else "never"
    )
    retry_at = status.retry_at.isoformat() if status.retry_at else "none"
    typer.echo(
        " ".join(
            (
                f"configured={str(status.configured).lower()}",
                f"auth={status.auth_status}",
                f"token={status.token_status}",
                f"last_success={last_success}",
                f"retry_at={retry_at}",
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
    token_store = TokenStore(settings.whoop_token_root)
    client = WhoopClient(
        oauth,
        token_store,
        str(profile_id),
        account,
    )
    with session_scope(build_engine(settings)) as session:
        report = sync_whoop(
            session,
            profile_id,
            account,
            client,
            full=full,
        )
    retry_at = report.retry_at.isoformat() if report.retry_at else "none"
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
                f"retry_at={retry_at}",
            )
        )
    )
    if report.status not in {"succeeded", "deferred"}:
        raise typer.Exit(code=1)


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
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="account-id") from error
    try:
        account = GmailAccount.create(
            account_id,
            initial_lookback_days=lookback_days,
            trusted_senders=(
                trusted_sender
                if trusted_sender is not None
                else (() if current is None else current.trusted_senders)
            ),
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
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
    oauth = GmailOAuth(settings.google_oauth_client_secrets, tokens)
    credentials = oauth.stage(
        profile.profile_id, account.account_id, force=True, interactive=True
    )
    mailbox = GoogleGmailGateway.from_credentials(
        credentials, timeout_seconds=settings.gmail_http_timeout_seconds
    ).get_profile()
    existing = tokens.load_verified(profile.profile_id, account.account_id)
    if existing is not None and existing[0] != mailbox.email:
        raise GmailAccountMismatch(
            f"Gmail account slot {account.account_id!r} is already bound"
        )
    oauth.publish_verified(
        profile.profile_id, account.account_id, credentials, mailbox.email
    )
    typer.echo(
        f"status=authorized profile={profile.profile_id} "
        f"account={account.account_id} email={mailbox.email}"
    )


@gmail_app.command("status")
def gmail_status(profile_id: UUID, account_id: str | None = None) -> None:
    """Show safe local Gmail connection and cursor status."""
    settings = Settings()
    profiles, tokens, state = _gmail_stores(settings)
    profile_key = str(profile_id)
    try:
        if not profiles.exists(profile_key):
            typer.echo(
                f"status=not_configured profile={profile_key} action_required=configure"
            )
            return
        profile = profiles.load(profile_key)
    except (OSError, RuntimeError, TypeError, ValueError):
        typer.echo(
            f"status=invalid_configuration profile={profile_key} "
            "action_required=repair_configuration",
            err=True,
        )
        raise typer.Exit(code=1) from None
    try:
        accounts = _selected_gmail_accounts(profile, account_id)
    except KeyError:
        typer.echo(
            f"status=not_configured profile={profile_key} account={account_id} "
            "action_required=configure"
        )
        return
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="account-id") from error
    for account in accounts:
        counts = state.counts(profile.profile_id, account.account_id)
        run = state.get_run_state(profile.profile_id, account.account_id)
        oauth = GmailOAuth(settings.google_oauth_client_secrets, tokens)
        token_status = oauth.local_status(profile.profile_id, account.account_id)
        # A successful re-authorization can happen after the previous sync
        # recorded ``oauth_required``.  Keep that historical run error visible,
        # but never let it override a currently valid credential.
        if run.last_error_code == "oauth_required" and token_status != "valid":
            token_status = "reauth_required"
        try:
            verified = tokens.load_verified(profile.profile_id, account.account_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            verified = None
        bound_email = None if verified is None else verified[0]
        typer.echo(
            " ".join(
                (
                    "status=configured",
                    f"profile={profile.profile_id}",
                    f"account={account.account_id}",
                    f"oauth={token_status}",
                    f"oauth_mode={settings.google_oauth_publishing_status}",
                    f"email={bound_email or 'unknown'}",
                    f"cursor={'ready' if state.get_cursor(profile.profile_id, account.account_id) else 'none'}",
                    f"messages={counts.get('messages', 0)}",
                    f"staged={counts.get('staged', 0)}",
                    f"medically_imported={counts.get('medically_imported', 0)}",
                    f"ocr_required={counts.get('ocr_required', 0)}",
                    f"attention={counts.get('attention_messages', 0) + counts.get('attention_attachments', 0)}",
                    f"last_attempt={run.last_attempt_at or 'never'}",
                    f"last_success={run.last_success_at or 'never'}",
                    f"last_error={run.last_error_code or 'none'}",
                )
            )
        )


@gmail_app.command("attention")
def gmail_attention(profile_id: UUID, account_id: str | None = None) -> None:
    """List safe identifiers for internally queued Gmail items."""
    settings = Settings()
    profiles, _, state = _gmail_stores(settings)
    profile = profiles.load(str(profile_id))
    for account in _selected_gmail_accounts(profile, account_id):
        for message_item in state.attention_messages(
            profile.profile_id, account.account_id
        ):
            typer.echo(
                f"status=attention profile={profile.profile_id} account={account.account_id} "
                f"message={message_item.message_id} kind=message "
                f"reason={message_item.classification}"
            )
        for attachment_item in state.attention_items(
            profile.profile_id, account.account_id
        ):
            reason = attachment_item.processing_status or attachment_item.outcome
            if reason == "needs_attention" and attachment_item.mime_type.startswith(
                "image/"
            ):
                reason = "image_ocr_required"
            typer.echo(
                f"status=attention profile={profile.profile_id} account={account.account_id} "
                f"message={attachment_item.message_id} part={attachment_item.part_id} "
                f"kind=attachment reason={reason or 'needs_attention'}"
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
    engine = build_engine(settings)
    for account in accounts:
        service_invoked = False
        with state.sync_lock(profile.profile_id, account.account_id):
            state.begin_sync(profile.profile_id, account.account_id, "preflight")
        try:
            if not tokens.exists(profile.profile_id, account.account_id):
                raise OAuthRequired("Gmail authorization is missing")
            oauth = GmailOAuth(settings.google_oauth_client_secrets, tokens)
            credentials = oauth.stage(profile.profile_id, account.account_id)
            verified = tokens.load_verified(profile.profile_id, account.account_id)
            if verified is None:
                raise OAuthRequired("Gmail authorization is missing")
            bound_email = verified[0]
            gateway = GoogleGmailGateway.from_credentials(
                credentials, timeout_seconds=settings.gmail_http_timeout_seconds
            )
            mailbox = gateway.get_profile()
            if mailbox.email != bound_email:
                raise GmailAccountMismatch("Gmail token does not match its binding")
            oauth.publish_verified(
                profile.profile_id, account.account_id, credentials, mailbox.email
            )
            service = GmailService(
                profile.profile_id,
                account.with_email(bound_email),
                gateway,
                state,
                MedicalAttachmentImporter(
                    profile.profile_id,
                    account.account_id,
                    engine,
                    FileVault(settings.vault_root),
                ),
                MedicalMessageInbox(
                    profile.profile_id,
                    account.account_id,
                    engine,
                ),
                SafeAttachmentPreparer(
                    settings.temporary_root,
                    settings.gmail_max_attachment_bytes,
                ),
            )
            service_invoked = True
            report = service.sync(full=full)
        except Exception as error:  # noqa: BLE001 - isolate configured accounts
            failed = True
            safe_error = (
                "oauth_required"
                if isinstance(error, OAuthRequired)
                else type(error).__name__
            )
            # GmailService records its own failure while still holding the sync
            # lock. Only preflight/OAuth failures need a separate state update.
            if not service_invoked:
                with state.sync_lock(profile.profile_id, account.account_id):
                    state.fail_sync(profile.profile_id, account.account_id, safe_error)
            typer.echo(
                f"status=failed profile={profile.profile_id} account={account.account_id} "
                f"safe_error={safe_error}"
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
                    f"staged={report.attachments_staged}",
                    f"medically_imported={report.medically_imported}",
                    f"duplicates={report.duplicates}",
                    f"ocr_required={report.ocr_required}",
                    f"attention={report.needs_attention}",
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


@question_app.command("ask")
def ask_health_question(
    question: str,
    profile_id: UUID = QUESTION_PROFILE_OPTION,
) -> None:
    """Answer one question using only the selected profile's verified context."""
    try:
        service = build_question_application(Settings())
    except Exception:  # noqa: BLE001 -- local configuration details stay private
        typer.echo(safe_question_setup_error(), err=True)
        raise typer.Exit(code=1) from None
    result = service.answer(profile_id, question)
    typer.echo(result.text, err=result.safe_error_code is not None)
    if result.safe_error_code is not None:
        raise typer.Exit(code=1)


@question_app.command("status")
def health_question_status(
    profile_id: UUID = QUESTION_PROFILE_OPTION,
) -> None:
    """Show safe readiness and source counts without printing health evidence."""
    try:
        status = question_status(Settings(), profile_id)
    except Exception:  # noqa: BLE001 -- configuration details must not cross the CLI
        typer.echo(
            f"status=unavailable profile_id={profile_id} error=question_unavailable",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if not status.available:
        typer.echo(
            f"status=unavailable profile_id={profile_id} "
            f"error={status.safe_error_code or 'question_unavailable'}",
            err=True,
        )
        raise typer.Exit(code=1)
    counts = " ".join(
        f"{source.value}={status.source_counts.get(source, 0)}"
        for source in EvidenceSource
    )
    typer.echo(f"status=ready readiness=local profile_id={profile_id} {counts}")


@reminder_app.command("propose")
def propose_health_reminder(
    profile_id: UUID,
    title: Annotated[str, typer.Option("--title")],
    reason: Annotated[str, typer.Option("--reason")],
    due: Annotated[str, typer.Option("--when")],
    source_type: Annotated[str, typer.Option("--source-type")],
    source_reference: Annotated[str, typer.Option("--source-reference")],
    timezone_name: Annotated[str, typer.Option("--timezone")] = "Europe/Moscow",
) -> None:
    """Create an inactive proposal; Telegram confirmation is still required."""
    try:
        due_at = parse_local_datetime(due, timezone_name)
        with session_scope(_reminder_engine()) as session:
            reminder = ReminderRepository(session).propose(
                profile_id=profile_id,
                title=title,
                reason=reason,
                source_type=source_type,
                source_reference=source_reference,
                due_at=due_at,
                timezone_name=timezone_name,
            )
    except Exception:  # noqa: BLE001 -- database details stay local
        typer.echo("status=failed safe_error=reminder_proposal_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"status={reminder.status.value} profile_id={profile_id} "
        f"code={reminder.public_code}"
    )


@reminder_app.command("list")
def list_health_reminders(profile_id: UUID) -> None:
    """List reminders for exactly one local profile."""
    try:
        with session_scope(_reminder_engine()) as session:
            reminders = ReminderRepository(session).list(profile_id)
    except Exception:  # noqa: BLE001 -- database details stay local
        typer.echo("status=failed safe_error=reminder_list_failed", err=True)
        raise typer.Exit(code=1) from None
    for reminder in reminders:
        typer.echo(
            f"code={reminder.public_code} status={reminder.status.value} "
            f"due_at={reminder.due_at.isoformat()} timezone={reminder.timezone_name} "
            f"title={_one_line(reminder.title)}"
        )


@reminder_app.command("status")
def health_reminder_status(profile_id: UUID) -> None:
    """Show content-free counts for one profile."""
    try:
        with session_scope(_reminder_engine()) as session:
            status = ReminderRepository(session).status(profile_id)
    except Exception:  # noqa: BLE001 -- database details stay local
        typer.echo("status=failed safe_error=reminder_status_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        " ".join(
            (
                "status=ready",
                f"profile_id={profile_id}",
                f"total={status.total}",
                f"pending_confirmation={status.pending_confirmation}",
                f"scheduled={status.scheduled}",
                f"due={status.due}",
                f"delivered={status.delivered}",
                f"completed={status.completed}",
                f"cancelled={status.cancelled}",
            )
        )
    )


@reminder_app.command("confirm")
def confirm_health_reminder(profile_id: UUID, code: str) -> None:
    _run_reminder_transition(profile_id, code, "confirm")


@reminder_app.command("complete")
def complete_health_reminder(profile_id: UUID, code: str) -> None:
    _run_reminder_transition(profile_id, code, "complete")


@reminder_app.command("cancel")
def cancel_health_reminder(profile_id: UUID, code: str) -> None:
    _run_reminder_transition(profile_id, code, "cancel")


@reminder_app.command("snooze")
def snooze_health_reminder(profile_id: UUID, code: str, duration: str) -> None:
    try:
        with session_scope(_reminder_engine()) as session:
            reminder = ReminderRepository(session).snooze(
                profile_id, code, duration=parse_snooze_duration(duration)
            )
    except Exception:  # noqa: BLE001 -- database details stay local
        typer.echo("status=failed safe_error=reminder_transition_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"status=scheduled profile_id={profile_id} code={code} "
        f"due_at={reminder.due_at.isoformat()}"
    )


@reminder_app.command("reschedule")
def reschedule_health_reminder(
    profile_id: UUID,
    code: str,
    due: Annotated[str, typer.Option("--when")],
    timezone_name: Annotated[str, typer.Option("--timezone")] = "Europe/Moscow",
) -> None:
    try:
        due_at = parse_local_datetime(due, timezone_name)
        with session_scope(_reminder_engine()) as session:
            reminder = ReminderRepository(session).reschedule(
                profile_id,
                code,
                due_at=due_at,
                timezone_name=timezone_name,
            )
    except Exception:  # noqa: BLE001 -- database details stay local
        typer.echo("status=failed safe_error=reminder_transition_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"status=scheduled profile_id={profile_id} code={code} "
        f"due_at={reminder.due_at.isoformat()}"
    )


@reminder_app.command("dispatch")
def dispatch_health_reminders(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Deliver pending proposals and due confirmed reminders once."""
    try:
        dispatcher, lock, paths = _reminder_dispatch_components(env_file)
        if not lock.acquire():
            typer.echo("status=skipped safe_error=already_running")
            return
        try:
            rotate_reminder_logs(paths)
            report = dispatcher.run()
        finally:
            lock.release()
    except Exception:  # noqa: BLE001 -- never leak credentials or private paths
        typer.echo("status=failed safe_error=reminder_dispatch_failed", err=True)
        raise typer.Exit(code=1) from None
    _print_reminder_dispatch_report(report)
    if report.failed:
        raise typer.Exit(code=1)


@reminder_app.command("render")
def render_health_reminder_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    _run_reminder_launchd("render", env_file)


@reminder_app.command("install")
def install_health_reminder_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    _run_reminder_launchd("install", env_file)


@reminder_app.command("automation-status")
def health_reminder_automation_status(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    _run_reminder_launchd("status", env_file)


@reminder_app.command("stop")
def stop_health_reminder_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    _run_reminder_launchd("stop", env_file)


@reminder_app.command("remove")
def remove_health_reminder_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    _run_reminder_launchd("remove", env_file)


@telegram_app.command("configure-token")
def configure_telegram_token() -> None:
    """Store a BotFather token locally without exposing it in shell history."""
    settings = Settings()
    token = typer.prompt("Bot token", hide_input=True)
    credential = _telegram_admin(settings).configure_token(token)
    typer.echo(
        " ".join(
            (
                "status=verified",
                f"bot_id={credential.bot_id}",
                f"bot_username={credential.username or ''}",
                f"token_file={settings.effective_telegram_token_file}",
            )
        )
    )


@telegram_app.command("run")
def run_telegram() -> None:
    """Run the bound private long-poller using only verified local credentials."""
    try:
        runtime = build_telegram_question_runtime(Settings())
        runtime.poller.validate_startup()
    except Exception:  # noqa: BLE001 -- never expose local credential/configuration data
        typer.echo("status=blocked error=telegram_runtime_unavailable", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("status=running")
    try:
        runtime.poller.run_forever()
    except KeyboardInterrupt:
        typer.echo("status=stopped")
    except Exception:  # noqa: BLE001 -- poller/state details may contain private paths
        typer.echo("status=blocked error=telegram_runtime_failed", err=True)
        raise typer.Exit(code=1) from None


@telegram_app.command("render")
def render_telegram_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Render the inspectable always-on Telegram LaunchAgent plist."""
    _run_telegram_launchd("render", env_file)


@telegram_app.command("install")
def install_telegram_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Install and load the always-on Telegram user LaunchAgent."""
    _run_telegram_launchd("install", env_file)


@telegram_app.command("automation-status")
def telegram_automation_status(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Report whether the Telegram LaunchAgent is loaded."""
    _run_telegram_launchd("status", env_file)


@telegram_app.command("stop")
def stop_telegram_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Unload Telegram automation while retaining managed files."""
    _run_telegram_launchd("stop", env_file)


@telegram_app.command("remove")
def remove_telegram_automation(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Unload Telegram automation and remove only its managed plists."""
    _run_telegram_launchd("remove", env_file)


@telegram_app.command("service-run", hidden=True)
def run_telegram_service(
    env_file: Annotated[Path, typer.Option("--env-file")],
) -> None:
    """Launchd-only singleton wrapper around the existing Telegram runner."""
    try:
        result = _telegram_service_runner(env_file).run()
    except Exception:  # noqa: BLE001 -- paths, settings and child details stay private
        typer.echo(
            "status=blocked error=telegram_service_configuration_failed", err=True
        )
        raise typer.Exit(code=1) from None
    if result.status == "already_running":
        typer.echo("status=skipped safe_error=already_running")
        return
    if result.returncode != 0:
        typer.echo("status=blocked error=telegram_service_failed", err=True)
        raise typer.Exit(code=result.returncode)
    typer.echo("status=stopped")


@telegram_app.command("bind")
def bind_telegram_identity(
    profile_id: UUID,
    telegram_user_id: int,
    private_chat_id: int | None = None,
) -> None:
    """Allow exactly one private Telegram identity for a profile."""
    identity = _telegram_admin(Settings()).bind_identity(
        profile_id, telegram_user_id, private_chat_id
    )
    typer.echo(
        " ".join(
            (
                "status=bound",
                f"profile_id={identity.profile_id}",
                f"telegram_user_id={identity.telegram_user_id}",
                f"private_chat_id={identity.private_chat_id}",
            )
        )
    )


@telegram_app.command("unbind")
def unbind_telegram_identity(profile_id: UUID) -> None:
    """Disable a profile's Telegram identity without deleting health data."""
    changed = _telegram_admin(Settings()).unbind_identity(profile_id)
    typer.echo(
        f"status={'unbound' if changed else 'not_bound'} profile_id={profile_id}"
    )


@telegram_app.command("status")
def telegram_status(profile_id: UUID | None = None) -> None:
    """Show local configuration/poll state without printing the bot token."""
    status = _telegram_admin(Settings()).status(profile_id)
    typer.echo(
        " ".join(
            (
                f"token_configured={str(status.token_configured).lower()}",
                f"credential_verified={str(status.credential_verified).lower()}",
                f"bot_id={status.bot_id or ''}",
                f"bot_username={status.bot_username or ''}",
                f"webhook_configured={'' if status.webhook_configured is None else str(status.webhook_configured).lower()}",
                f"poller_running={str(status.poller_running).lower()}",
                f"delivery_unknown_count={status.delivery_unknown_count}",
                f"profile_id={status.profile_id or ''}",
                f"identity_bound={str(status.identity_bound).lower()}",
                f"next_offset={status.next_offset if status.next_offset is not None else ''}",
                f"last_poll_at={status.last_poll_at.isoformat() if status.last_poll_at else ''}",
                f"last_error_code={status.last_error_code or ''}",
            )
        )
    )


@telegram_app.command("discover-id")
def discover_telegram_id() -> None:
    """List private sender/chat IDs from pending updates; never print message text."""
    settings = Settings()
    credential = PrivateBotTokenStore(
        settings.effective_telegram_token_file
    ).load_verified()
    gateway = TelegramBotAPI(credential.token)
    remote = gateway.get_me()
    if remote.get("id") != credential.bot_id or remote.get("is_bot") is not True:
        typer.echo("status=blocked error=bot_identity_mismatch", err=True)
        raise typer.Exit(code=1)
    if gateway.get_webhook_url():
        typer.echo("status=blocked error=webhook_configured", err=True)
        raise typer.Exit(code=1)
    updates = gateway.get_updates(
        offset=SqliteTelegramState(settings.telegram_state_file).next_offset(
            credential.bot_id
        ),
        timeout_seconds=1,
    )
    candidates: set[tuple[int, int]] = set()
    for update in updates:
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            continue
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if (
            chat.get("type") == "private"
            and isinstance(user_id, int)
            and not isinstance(user_id, bool)
            and isinstance(chat_id, int)
            and not isinstance(chat_id, bool)
        ):
            candidates.add((user_id, chat_id))
    if not candidates:
        typer.echo("status=no_private_messages")
        return
    for user_id, chat_id in sorted(candidates):
        typer.echo(f"telegram_user_id={user_id} private_chat_id={chat_id}")


def _drive_stores(
    settings: Settings,
) -> tuple[LocalProfileStore, LocalTokenStore, LocalSyncStateStore]:
    return (
        LocalProfileStore(settings.google_drive_root),
        LocalTokenStore(settings.google_drive_root),
        LocalSyncStateStore(settings.google_drive_root),
    )


@drive_app.command("configure")
def configure_drive(profile_id: UUID, folders: list[str]) -> None:
    """Configure one or more read-only source folders for a local profile."""
    settings = Settings()
    profiles, _, state = _drive_stores(settings)
    _require_database_profile(settings, profile_id)
    profile_key = str(profile_id)
    profile = DriveProfile.create(profile_key, folders)
    with state.sync_lock(profile_key):
        if profiles.exists(profile_key):
            current = profiles.load(profile_key)
            roots_changed = current.root_folder_ids != profile.root_folder_ids
            if (
                current.account_permission_id is not None
                and current.account_email is not None
            ):
                profile = profile.with_account(
                    current.account_permission_id, current.account_email
                )
        else:
            roots_changed = True
        if roots_changed:
            state.clear_cursor(profile_key)
        profiles.save(profile)
    typer.echo(
        f"status=configured profile={profile.profile_id} roots={len(profile.root_folder_ids)}"
    )


@drive_app.command("auth")
def authorize_drive(profile_id: UUID) -> None:
    """Authorize one Google account using a local Desktop OAuth callback."""
    settings = Settings()
    profiles, tokens, state = _drive_stores(settings)
    _require_database_profile(settings, profile_id)
    profile_key = str(profile_id)
    profiles.load(profile_key)
    oauth = DriveOAuth(
        settings.google_drive_client_secrets,
        tokens,
        settings.google_drive_http_timeout_seconds,
    )
    credentials = oauth.stage(profile_key, force=True, interactive=True)
    identity = GoogleDriveGateway.from_credentials(
        credentials, timeout_seconds=settings.google_drive_http_timeout_seconds
    ).account_identity()
    previous = tokens.load_verified(profile_key)
    if previous is not None and previous[0].permission_id != identity.permission_id:
        raise DriveProfileMismatch(
            f"profile {profile_key!r} is already bound to another Google account"
        )
    oauth.publish_verified(profile_key, credentials, identity)
    with state.sync_lock(profile_key):
        profile = profiles.load(profile_key).with_account(
            identity.permission_id, identity.email
        )
        profiles.save(profile)
    typer.echo(f"status=authorized profile={profile_key} account={identity.email}")


@drive_app.command("status")
def drive_status(profile_id: UUID) -> None:
    """Show safe local Drive configuration and synchronization freshness."""
    settings = Settings()
    profiles, tokens, state = _drive_stores(settings)
    _require_database_profile(settings, profile_id)
    profile_key = str(profile_id)
    profile = profiles.load(profile_key)
    oauth = DriveOAuth(
        settings.google_drive_client_secrets,
        tokens,
        settings.google_drive_http_timeout_seconds,
    )
    try:
        verified = tokens.load_verified(profile_key)
    except (OSError, RuntimeError, TypeError, ValueError):
        verified = None
    counts = state.counts(profile_key)
    run = state.run_state(profile_key)
    attention = sum(
        count
        for outcome, count in counts.items()
        if outcome
        in {
            "needs_attention",
            "too_large",
            "processing_failed",
            "transient_download_failed",
            "download_failed",
            "not_found",
            "download_restricted",
            "unsupported_google_native",
            "unsupported_media_type",
        }
    )
    typer.echo(
        " ".join(
            (
                "status=configured",
                f"profile={profile.profile_id}",
                f"token={oauth.local_status(profile_key)}",
                f"account_bound={'yes' if verified is not None else 'no'}",
                f"account={verified[0].email if verified is not None else 'unknown'}",
                f"roots={len(profile.root_folder_ids)}",
                f"root_accessible={run['root_accessible'] or 'unknown'}",
                f"sync_state={'interrupted' if run['in_progress'] == 'yes' else 'idle'}",
                f"cursor={'ready' if state.get_cursor(profile_key) else 'none'}",
                f"medically_imported={counts.get('medically_imported', 0)}",
                f"ocr_required={counts.get('ocr_required', 0)}",
                f"attention={attention}",
                f"action_required={attention + counts.get('ocr_required', 0)}",
                f"last_attempt={run['last_attempt_at'] or 'never'}",
                f"last_success={run['last_success_at'] or 'never'}",
                f"last_error={run['last_error_code'] or 'none'}",
            )
        )
    )


@drive_app.command("sync")
def sync_drive(profile_id: UUID, full: bool = False) -> None:
    """Import new or changed Drive files into the medical pipeline."""
    settings = Settings()
    profiles, tokens, state = _drive_stores(settings)
    _require_database_profile(settings, profile_id)
    profile_key = str(profile_id)
    oauth = DriveOAuth(
        settings.google_drive_client_secrets,
        tokens,
        settings.google_drive_http_timeout_seconds,
    )
    credentials = oauth.stage(profile_key)
    verified = tokens.load_verified(profile_key)
    if verified is None:
        raise RuntimeError(
            f"Google Drive profile {profile_key!r} needs OAuth authorization"
        )
    identity = verified[0]
    with state.sync_lock(profile_key):
        profile = profiles.load(profile_key).with_account(
            identity.permission_id, identity.email
        )
        profiles.save(profile)
        service = DriveService(
            profile,
            GoogleDriveGateway.from_credentials(
                credentials,
                timeout_seconds=settings.google_drive_http_timeout_seconds,
            ),
            state,
            MedicalDriveConsumer(
                profile_key,
                build_engine(settings),
                FileVault(settings.vault_root),
                settings.temporary_root,
            ),
        )
        report = service.sync(full=full, lock_already_held=True)
    oauth.publish_verified(profile_key, credentials, identity)
    typer.echo(
        " ".join(
            (
                "status=synced",
                f"profile={report.profile_id}",
                f"mode={report.mode}",
                f"discovered={report.discovered}",
                f"medically_imported={report.medically_imported}",
                f"duplicates={report.duplicates}",
                f"ocr_required={report.ocr_required}",
                f"attention={report.needs_attention}",
                f"failed={report.failed}",
                f"unchanged={report.unchanged}",
                f"skipped={report.skipped}",
                f"removed={report.removed}",
            )
        )
    )


@sheets_app.command("configure")
def configure_sheets(
    profile_id: UUID,
    reuse_drive_binding: bool = typer.Option(
        True, "--reuse-drive-binding/--no-reuse-drive-binding"
    ),
    reset_unknown_creation: bool = typer.Option(
        False,
        "--reset-unknown-creation",
        help="Allow one new workbook create after checking Drive for an orphan.",
    ),
) -> None:
    """Configure one generated spreadsheet for a local health profile."""
    settings = Settings()
    permission_id: str | None = None
    email: str | None = None
    try:
        if reuse_drive_binding:
            verified = LocalTokenStore(settings.google_drive_root).load_verified(
                str(profile_id)
            )
            if verified is not None:
                permission_id = verified[0].permission_id
                email = verified[0].email
        profile = _build_sheets_service(settings).configure(
            profile_id,
            expected_permission_id=permission_id,
            expected_email=email,
            reset_unknown_creation=reset_unknown_creation,
        )
    except Exception:  # noqa: BLE001 - local paths/accounts stay private
        typer.echo("status=failed safe_error=sheets_configuration_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"status=configured profile={profile.profile_id} "
        f"account_bound={'yes' if profile.expected_permission_id else 'no'} "
        f"spreadsheet={'ready' if profile.spreadsheet_id else 'pending'}"
    )


@sheets_app.command("authorize")
def authorize_sheets(profile_id: UUID, force: bool = False) -> None:
    """Authorize exact Sheets scopes and verify the selected Google account."""
    try:
        _build_sheets_service(Settings()).authorize(
            profile_id, force=force, interactive=True
        )
    except Exception:  # noqa: BLE001 - OAuth details stay private
        typer.echo("status=failed safe_error=sheets_authorization_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status=authorized profile={profile_id}")


@sheets_app.command("sync")
def sync_sheets(profile_id: UUID) -> None:
    """Import review decisions and refresh managed profile projections."""
    try:
        report = _build_sheets_service(Settings()).sync(profile_id)
    except WorkbookOwnershipError:
        typer.echo("status=failed safe_error=workbook_mismatch", err=True)
        raise typer.Exit(code=1) from None
    except SheetsSyncFailure as error:
        typer.echo(f"status=failed safe_error={error.safe_code}", err=True)
        raise typer.Exit(code=1) from None
    except Exception:  # noqa: BLE001 - remote cells and API bodies stay private
        typer.echo("status=failed safe_error=sheets_sync_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        " ".join(
            (
                "status=succeeded",
                f"profile={profile_id}",
                f"spreadsheet={report.spreadsheet_id}",
                f"decisions={report.decisions_applied}",
                f"replayed={report.decisions_replayed}",
                f"labs={report.lab_rows}",
                f"review={report.review_rows}",
                f"sources={report.source_rows}",
            )
        )
    )


@sheets_app.command("status")
def sheets_status(profile_id: UUID) -> None:
    """Show local Sheets readiness without refreshing OAuth or using the network."""
    try:
        status = _build_sheets_service(Settings()).status(profile_id)
    except Exception:  # noqa: BLE001 - local configuration details stay private
        typer.echo("status=failed safe_error=sheets_status_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        " ".join(
            (
                f"status={'configured' if status.configured else 'missing'}",
                f"profile={profile_id}",
                f"oauth={status.authorization}",
                f"spreadsheet={'ready' if status.spreadsheet_configured else 'pending'}",
                f"last_sync={status.last_status or 'never'}",
                f"last_success={status.last_success_at or 'never'}",
                f"last_error={status.safe_error_code or 'none'}",
            )
        )
    )


def _require_database_profile(settings: Settings, profile_id: UUID) -> None:
    with session_scope(build_engine(settings)) as session:
        if session.scalar(select(Profile.id).where(Profile.id == profile_id)) is None:
            raise typer.BadParameter("profile does not exist in the health database")


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
    try:
        client_id, client_secret = settings.load_whoop_client_credentials()
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    return WhoopOAuth(
        client_id,
        client_secret.get_secret_value(),
        settings.whoop_redirect_uri,
    )


def _telegram_admin(settings: Settings) -> TelegramAdminService:
    return TelegramAdminService(
        PrivateBotTokenStore(settings.effective_telegram_token_file),
        SqliteTelegramState(settings.telegram_state_file),
        DatabaseProfileDirectory(settings),
    )


def _build_sheets_service(settings: Settings) -> SheetsService:
    profiles = LocalSheetsProfileStore(settings.google_sheets_root)
    tokens = LocalSheetsTokenStore(settings.google_sheets_root)
    state = LocalSheetsStateStore(settings.google_sheets_root)

    def gateway(credentials):  # type: ignore[no-untyped-def]
        return GoogleSheetsGateway.from_credentials(
            credentials,
            timeout_seconds=settings.google_sheets_http_timeout_seconds,
        )

    oauth = SheetsOAuth(
        settings.google_sheets_client_secrets,
        profiles,
        tokens,
        gateway,
        timeout_seconds=settings.google_sheets_http_timeout_seconds,
    )
    engine = build_engine(settings)
    return SheetsService(
        profiles,
        state,
        oauth,
        gateway,
        lambda: session_scope(engine),
        lambda session, profile_id: collect_source_statuses(
            settings, session, profile_id, sheets_oauth=oauth
        ),
    )


def _profile_exists(settings: Settings, profile_id: UUID) -> bool:
    with session_scope(build_engine(settings)) as session:
        return (
            session.scalar(select(Profile.id).where(Profile.id == profile_id))
            is not None
        )


def _staging_manager(env_file: Path | None) -> StagingManager:
    root = Path(__file__).resolve().parents[2]
    try:
        environment = StagingEnvironment.load(root, env_file)
    except StagingConfigurationError as error:
        raise typer.BadParameter(str(error), param_hint="--env-file") from error
    return StagingManager(environment)


def _automation_components(
    env_file: Path,
) -> tuple[AutomationRunner, LaunchdManager, LaunchdPaths]:
    repository_root = Path(__file__).resolve().parents[2]
    executable = _current_console_script()
    expanded_env_file = env_file.expanduser()
    if not expanded_env_file.is_absolute():
        raise ValueError("env_file_not_absolute")
    resolved_env_file = expanded_env_file.resolve()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=resolved_env_file
    )
    settings.gmail_root = _repository_relative(repository_root, settings.gmail_root)
    settings.google_drive_root = _repository_relative(
        repository_root, settings.google_drive_root
    )
    settings.google_sheets_root = _repository_relative(
        repository_root, settings.google_sheets_root
    )
    settings.automation_root = _repository_relative(
        repository_root, settings.automation_root
    )
    paths = LaunchdPaths.resolve(
        automation_root=settings.automation_root,
        executable=executable,
        environment_file=expanded_env_file,
        working_directory=repository_root,
    )
    runner = AutomationRunner(
        settings,
        configured_job_adapters(settings, paths.executable),
        SubprocessJobExecutor(
            paths.executable, paths.environment_file, paths.working_directory
        ),
        AutomationState(paths.state_file),
        GlobalRunLock(paths.lock_file),
        before_jobs=lambda: rotate_safe_logs(paths),
    )
    return runner, LaunchdManager(paths), paths


def _automation_manager_or_exit(env_file: Path) -> LaunchdManager:
    try:
        _, manager, _ = _automation_components(env_file)
        return manager
    except (RuntimeError, ValueError):
        typer.echo("status=failed safe_error=automation_configuration_failed", err=True)
        raise typer.Exit(code=1) from None


def _telegram_launchd_paths(env_file: Path) -> TelegramLaunchdPaths:
    repository_root = Path(__file__).resolve().parents[2]
    expanded = env_file.expanduser()
    require_private_file(expanded)
    resolved = expanded.resolve()
    settings = Settings(_env_file=resolved)  # type: ignore[call-arg]
    automation_root = _repository_relative(repository_root, settings.automation_root)
    return TelegramLaunchdPaths.resolve(
        automation_root=automation_root,
        executable=_current_console_script(),
        environment_file=resolved,
        working_directory=repository_root,
    )


def _telegram_launchd_manager(env_file: Path) -> TelegramLaunchdManager:
    return TelegramLaunchdManager(_telegram_launchd_paths(env_file))


def _telegram_service_runner(env_file: Path) -> TelegramServiceRunner:
    return TelegramServiceRunner(_telegram_launchd_paths(env_file))


def _run_telegram_launchd(action: str, env_file: Path) -> None:
    try:
        manager = _telegram_launchd_manager(env_file)
        if action == "render":
            manager.render()
            status = "rendered"
        elif action == "install":
            status = manager.install()
        elif action == "status":
            status = manager.status()
        elif action == "stop":
            status = manager.stop()
        elif action == "remove":
            status = manager.remove()
        else:
            raise ValueError("invalid_launchd_action")
    except TelegramLaunchdError as error:
        typer.echo(f"status=failed safe_error={error.safe_code}", err=True)
        raise typer.Exit(code=1) from None
    except Exception:  # noqa: BLE001 -- never expose credentials or private paths
        typer.echo("status=failed safe_error=telegram_launchd_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={status} label={TELEGRAM_LABEL}")


def _reminder_engine():
    return build_engine(Settings())


def _run_reminder_transition(profile_id: UUID, code: str, action: str) -> None:
    try:
        with session_scope(_reminder_engine()) as session:
            repository = ReminderRepository(session)
            if action == "confirm":
                reminder = repository.confirm(profile_id, code)
            elif action == "complete":
                reminder = repository.complete(profile_id, code)
            elif action == "cancel":
                reminder = repository.cancel(profile_id, code)
            else:
                raise ValueError("invalid_reminder_action")
    except Exception:  # noqa: BLE001 -- database details stay local
        typer.echo("status=failed safe_error=reminder_transition_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={reminder.status.value} profile_id={profile_id} code={code}")


def _reminder_settings(env_file: Path) -> tuple[Settings, Path, Path]:
    repository_root = Path(__file__).resolve().parents[2]
    expanded = env_file.expanduser()
    if not expanded.is_absolute():
        raise ValueError("env_file_not_absolute")
    resolved = expanded.resolve()
    from health_agent.automation.storage import require_private_file

    require_private_file(resolved)
    settings = Settings(_env_file=resolved)  # type: ignore[call-arg]
    settings.automation_root = _repository_relative(
        repository_root, settings.automation_root
    )
    settings.telegram_root = _repository_relative(
        repository_root, settings.telegram_root
    )
    if settings.telegram_token_file is not None:
        settings.telegram_token_file = _repository_relative(
            repository_root, settings.telegram_token_file
        )
    if settings.telegram_state_path is not None:
        settings.telegram_state_path = _repository_relative(
            repository_root, settings.telegram_state_path
        )
    return settings, repository_root, resolved


def _reminder_dispatch_components(
    env_file: Path,
) -> tuple[ReminderDispatcher, GlobalRunLock, ReminderLaunchdPaths]:
    settings, repository_root, resolved_env = _reminder_settings(env_file)
    credential = PrivateBotTokenStore(
        settings.effective_telegram_token_file
    ).load_verified()
    state = SqliteTelegramState(settings.telegram_state_file)
    state.register_bot(credential.bot_id, credential.username)
    gateway = TelegramBotAPI(credential.token)
    messenger = TelegramMessenger(credential.bot_id, gateway, state)
    dispatcher = ReminderDispatcher(build_engine(settings), messenger)
    paths = ReminderLaunchdPaths.resolve(
        automation_root=settings.automation_root,
        executable=_current_console_script(),
        environment_file=resolved_env,
        working_directory=repository_root,
    )
    return dispatcher, GlobalRunLock(paths.lock_file), paths


def _reminder_launchd_manager(env_file: Path) -> ReminderLaunchdManager:
    settings, repository_root, resolved_env = _reminder_settings(env_file)
    paths = ReminderLaunchdPaths.resolve(
        automation_root=settings.automation_root,
        executable=_current_console_script(),
        environment_file=resolved_env,
        working_directory=repository_root,
    )
    return ReminderLaunchdManager(paths)


def _run_reminder_launchd(action: str, env_file: Path) -> None:
    try:
        manager = _reminder_launchd_manager(env_file)
        if action == "render":
            manager.render()
            status = "rendered"
        elif action == "install":
            status = manager.install()
        elif action == "status":
            status = manager.status()
        elif action == "stop":
            status = manager.stop()
        elif action == "remove":
            status = manager.remove()
        else:
            raise ValueError("invalid_launchd_action")
    except Exception:  # noqa: BLE001 -- never leak private filesystem details
        typer.echo("status=failed safe_error=reminder_launchd_failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"status={status} label={REMINDER_LABEL}")


def _print_reminder_dispatch_report(report: DispatchReport) -> None:
    typer.echo(
        " ".join(
            (
                "status=succeeded" if not report.failed else "status=failed",
                f"proposals_sent={report.proposals_sent}",
                f"proposals_acknowledged={report.proposals_acknowledged}",
                f"due_sent={report.due_sent}",
                f"due_acknowledged={report.due_acknowledged}",
                f"failed={report.failed}",
            )
        )
    )


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _repository_relative(repository_root: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else repository_root / expanded


def _current_console_script() -> Path:
    invoked = Path(sys.argv[0]).expanduser()
    if (
        invoked.is_absolute()
        and invoked.name == "health-agent"
        and invoked.is_file()
        and os.access(invoked, os.X_OK)
    ):
        return invoked.resolve()
    sibling = Path(sys.executable).parent / "health-agent"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling.resolve()
    discovered = shutil.which("health-agent")
    if discovered is not None:
        return Path(discovered).resolve()
    raise ValueError("health_agent_executable_unavailable")
