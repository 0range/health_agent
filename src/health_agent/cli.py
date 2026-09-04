from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from sqlalchemy import select

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
    Profile,
    ReviewStatus,
    SourceRecord,
)
from health_agent.telegram.admin import DatabaseProfileDirectory, TelegramAdminService
from health_agent.telegram.api import TelegramBotAPI
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.vault import FileVault
from health_agent.whoop.auth_service import (
    complete_whoop_authorization,
    open_and_wait_for_whoop_authorization,
    publish_whoop_authorization,
    validate_whoop_authorization_target,
)
from health_agent.whoop.client import WhoopClient
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
app.add_typer(review_app, name="review")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(whoop_app, name="whoop")
app.add_typer(profile_app, name="profile")
app.add_typer(gmail_app, name="gmail")
app.add_typer(telegram_app, name="telegram")


@app.callback()
def health_agent() -> None:
    """Personal Health Agent."""


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
        profiles = session.scalars(select(Profile).order_by(Profile.created_at)).all()
    for profile in profiles:
        typer.echo(f"profile_id={profile.id} name={profile.name}")


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
    profile = profiles.load(str(profile_id))
    accounts = _selected_gmail_accounts(profile, account_id)
    for account in accounts:
        counts = state.counts(profile.profile_id, account.account_id)
        run = state.get_run_state(profile.profile_id, account.account_id)
        oauth = GmailOAuth(settings.google_oauth_client_secrets, tokens)
        token_status = oauth.local_status(profile.profile_id, account.account_id)
        if run.last_error_code == "oauth_required":
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
