"""Profile-scoped connector status assembly for the local management panel."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.gmail.config import GmailProfile
from health_agent.gmail.oauth import GmailOAuth
from health_agent.gmail.stores import (
    LocalGmailProfileStore,
    LocalGmailStateStore,
    LocalGmailTokenStore,
)
from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.stores import (
    LocalProfileStore,
    LocalSyncStateStore,
    LocalTokenStore,
)
from health_agent.google_sheets.stores import LocalSheetsProfileStore
from health_agent.models import Profile
from health_agent.panel.models import (
    ConnectorCard,
    PanelDestination,
    ProfilePanel,
    ProfileSummary,
)
from health_agent.reminders.repository import ReminderRepository
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import TelegramStatus
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.status import WhoopStatus, get_whoop_status
from health_agent.whoop.tokens import TokenStore

SessionScopeFactory = Callable[[], AbstractContextManager[Session]]
DestinationFactory = Callable[[UUID], tuple[PanelDestination, ...]]

_WHOOP_ERROR_CODES = frozenset({"reauth_required", "rate_limited", "sync_failed"})
_GMAIL_ERROR_CODES = frozenset(
    {
        "AttachmentPreparationError",
        "GmailAccountMismatch",
        "GmailPaginationLoop",
        "OAuthRequired",
    }
)
_TELEGRAM_ERROR_CODES = frozenset({"credential_invalid", "token_not_configured"})


class ProfileRepository(Protocol):
    def list(self) -> tuple[ProfileSummary, ...]: ...

    def get(self, profile_id: UUID) -> ProfileSummary | None: ...

    def create(self, name: str) -> ProfileSummary: ...


class ConnectorStatusReader(Protocol):
    connector: str

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]: ...


class DriveConfigurationPort(ConnectorStatusReader, Protocol):
    def folder_ids(self, profile_id: UUID) -> tuple[str, ...]: ...

    def configure(self, profile_id: UUID, folders: list[str]) -> None: ...


class ProfileNotFoundError(LookupError):
    """The requested profile is not present in the local database."""


class SqlAlchemyProfileRepository:
    """Small database adapter that exposes only panel-safe profile fields."""

    def __init__(self, sessions: SessionScopeFactory) -> None:
        self._sessions = sessions

    def list(self) -> tuple[ProfileSummary, ...]:
        with self._sessions() as session:
            return tuple(
                _summary(profile)
                for profile in session.scalars(
                    select(Profile).order_by(Profile.created_at, Profile.id)
                )
            )

    def get(self, profile_id: UUID) -> ProfileSummary | None:
        with self._sessions() as session:
            profile = session.get(Profile, profile_id)
            return None if profile is None else _summary(profile)

    def create(self, name: str) -> ProfileSummary:
        normalized_name = _profile_name(name)
        profile = Profile(id=uuid4(), name=normalized_name)
        with self._sessions() as session:
            session.add(profile)
            session.flush()
            return _summary(profile)


class WhoopStatusReader:
    """Reads each selected profile's WHOOP account state without token values."""

    connector = "whoop"

    def __init__(self, sessions: SessionScopeFactory, tokens: TokenStore) -> None:
        self._sessions = sessions
        self._tokens = tokens

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        with self._sessions() as session:
            accounts = tuple(
                session.scalars(
                    select(WhoopConnection.account_name)
                    .where(WhoopConnection.profile_id == profile_id)
                    .order_by(WhoopConnection.account_name)
                )
            )
            if not accounts:
                return (
                    ConnectorCard(
                        self.connector,
                        "not_connected",
                        "К этому профилю не подключён ни один аккаунт WHOOP.",
                        None,
                        None,
                    ),
                )
            statuses = tuple(
                get_whoop_status(
                    session, self._tokens, profile_id, str(profile_id), account
                )
                for account in accounts
            )
        return (_whoop_card(statuses, accounts),)


class GmailStatusReader:
    """Reads local Gmail configuration and sync state for one profile only."""

    connector = "gmail"

    def __init__(
        self,
        profiles: LocalGmailProfileStore,
        state: LocalGmailStateStore,
        oauth_status: Callable[[str, str], str],
    ) -> None:
        self._profiles = profiles
        self._state = state
        self._oauth_status = oauth_status

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        profile_key = str(profile_id)
        if not self._profiles.exists(profile_key):
            return (
                ConnectorCard(
                    self.connector,
                    "not_configured",
                    "Для этого профиля не настроен ни один аккаунт Gmail.",
                    None,
                    None,
                ),
            )
        profile = self._profiles.load(profile_key)
        return (_gmail_card(profile, self._state, self._oauth_status),)


class TelegramStatusReader:
    """Maps an injected profile-scoped Telegram status to a safe panel card."""

    connector = "telegram"

    def __init__(self, status: Callable[[UUID], TelegramStatus]) -> None:
        self._status = status

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        status = self._status(profile_id)
        if not status.token_configured:
            return (
                ConnectorCard(
                    self.connector,
                    "not_configured",
                    "Telegram не настроен локально.",
                    None,
                    _panel_error_code("telegram", status.last_error_code),
                ),
            )
        if not status.credential_verified:
            return (
                ConnectorCard(
                    self.connector,
                    "credential_invalid",
                    "Учётные данные Telegram нужно проверить локально.",
                    None,
                    _panel_error_code("telegram", status.last_error_code),
                ),
            )
        if not status.identity_bound:
            return (
                ConnectorCard(
                    self.connector,
                    "not_bound",
                    "К этому профилю не привязана учётная запись Telegram.",
                    None,
                    _panel_error_code("telegram", status.last_error_code),
                ),
            )
        return (
            ConnectorCard(
                self.connector,
                "ready" if status.poller_running else "configured",
                (
                    "Опрос Telegram активен для этого профиля."
                    if status.poller_running
                    else "Telegram настроен для этого профиля."
                ),
                # Telegram's poll timestamp is bot-global, never profile-scoped.
                None,
                _panel_error_code("telegram", status.last_error_code),
            ),
        )


class ReminderStatusReader:
    """Expose profile-scoped reminder counts without reminder content."""

    connector = "reminders"

    def __init__(self, sessions: SessionScopeFactory) -> None:
        self._sessions = sessions

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        with self._sessions() as session:
            summary = ReminderRepository(session).status(profile_id)
        status = (
            "action_required"
            if summary.pending_confirmation or summary.due
            else "ready"
        )
        detail = (
            f"Ожидают подтверждения: {summary.pending_confirmation} · "
            f"Запланировано: {summary.scheduled} · Пора отправить: {summary.due}"
        )
        return (ConnectorCard(self.connector, status, detail),)


class DatabaseStatusReader:
    """Check only local database availability; never count health rows."""

    connector = "database"

    def __init__(self, sessions: SessionScopeFactory) -> None:
        self._sessions = sessions

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        del profile_id
        with self._sessions() as session:
            session.execute(select(1)).scalar_one()
        return (ConnectorCard(self.connector, "ready", "Локальная база доступна."),)


class DriveConfiguration:
    """Read and update one profile's local Drive roots without remote calls."""

    connector = "drive"

    def __init__(
        self,
        profiles: LocalProfileStore,
        tokens: LocalTokenStore,
        state: LocalSyncStateStore,
    ) -> None:
        self._profiles = profiles
        self._tokens = tokens
        self._state = state

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        profile_key = str(profile_id)
        if not self._profiles.exists(profile_key):
            return (
                ConnectorCard(
                    self.connector,
                    "not_configured",
                    "Для этого профиля не настроена папка Google Drive.",
                ),
            )
        profile = self._profiles.load(profile_key)
        run = self._state.run_state(profile_key)
        last_success = (
            _parse_datetime(run["last_success_at"]) if run["last_success_at"] else None
        )
        verified = self._tokens.load_verified(profile_key)
        token_identity = verified[0] if verified is not None else None
        authorized = (
            token_identity is not None
            and profile.account_permission_id is not None
            and profile.account_email is not None
            and token_identity.permission_id == profile.account_permission_id
            and token_identity.email == profile.account_email
        )
        if not authorized:
            status = "needs_authorization"
        elif last_success is None:
            status = "configured"
        else:
            status = "ready"
        error_code = "drive_status_error" if run["last_error_code"] else None
        detail = f"Папок Google Drive в профиле: {len(profile.root_folder_ids)}."
        if not authorized:
            detail += " Требуется авторизация Google."
        return (
            ConnectorCard(
                self.connector,
                status,
                detail,
                last_success,
                error_code,
            ),
        )

    def folder_ids(self, profile_id: UUID) -> tuple[str, ...]:
        profile_key = str(profile_id)
        if not self._profiles.exists(profile_key):
            return ()
        return self._profiles.load(profile_key).root_folder_ids

    def configure(self, profile_id: UUID, folders: list[str]) -> None:
        profile_key = str(profile_id)
        candidate = DriveProfile.create(profile_key, folders)
        with self._state.sync_lock(profile_key):
            current = (
                self._profiles.load(profile_key)
                if self._profiles.exists(profile_key)
                else None
            )
            if (
                current is not None
                and current.account_permission_id is not None
                and current.account_email is not None
            ):
                candidate = candidate.with_account(
                    current.account_permission_id, current.account_email
                )
            if current is None or current.root_folder_ids != candidate.root_folder_ids:
                self._state.clear_cursor(profile_key)
            self._profiles.save(candidate)


class PanelService:
    """Build safe, isolated profile panels from injected repositories/readers."""

    def __init__(
        self,
        profiles: ProfileRepository,
        readers: tuple[ConnectorStatusReader, ...] | list[ConnectorStatusReader] = (),
        *,
        drive: DriveConfigurationPort | None = None,
        destinations: tuple[PanelDestination, ...] = (),
        destination_factory: DestinationFactory | None = None,
    ) -> None:
        self._profiles = profiles
        self._readers = tuple(readers)
        self._drive = drive
        self._destinations = destinations
        self._destination_factory = destination_factory

    def list_profiles(self) -> tuple[ProfileSummary, ...]:
        return self._profiles.list()

    def create_profile(self, name: str) -> ProfileSummary:
        return self._profiles.create(_profile_name(name))

    def profile(self, profile_id: UUID) -> ProfilePanel:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ProfileNotFoundError(str(profile_id))
        cards = tuple(
            card
            for reader in self._readers
            for card in self._safe_cards(reader, profile_id)
        )
        drive_cards: tuple[ConnectorCard, ...]
        if self._drive is None:
            drive_cards = (_drive_card(),)
            drive_folder_ids: tuple[str, ...] = ()
        else:
            drive_cards = self._safe_cards(self._drive, profile_id)
            try:
                drive_folder_ids = self._drive.folder_ids(profile_id)
            except Exception:  # noqa: BLE001 - keep local state failures off the page.
                drive_folder_ids = ()
        destinations = self._destinations
        if self._destination_factory is not None:
            try:
                destinations = (*destinations, *self._destination_factory(profile_id))
            except Exception:  # noqa: BLE001 - local state details stay off the page.
                destinations = (
                    *destinations,
                    PanelDestination(
                        "google_sheets",
                        "Google Таблица",
                        None,
                        "Статус Google Таблицы временно недоступен",
                    ),
                )
        return ProfilePanel(
            profile=profile,
            connectors=(*cards, *drive_cards),
            drive_folder_ids=drive_folder_ids,
            destinations=destinations,
        )

    def configure_drive(self, profile_id: UUID, folders: list[str]) -> None:
        if self._profiles.get(profile_id) is None:
            raise ProfileNotFoundError(str(profile_id))
        if self._drive is None:
            raise RuntimeError("Google Drive configuration is unavailable")
        self._drive.configure(profile_id, folders)

    @staticmethod
    def _safe_cards(
        reader: ConnectorStatusReader, profile_id: UUID
    ) -> tuple[ConnectorCard, ...]:
        try:
            return reader.cards(profile_id)
        except Exception:  # noqa: BLE001 - a broken card must not break the panel.
            return (
                ConnectorCard(
                    connector=reader.connector,
                    status="status_unavailable",
                    detail="Локальный статус коннектора недоступен.",
                    last_success_at=None,
                    error_code="local_status_unavailable",
                ),
            )


def build_panel_service(settings: Settings) -> PanelService:
    """Build production adapters without invoking OAuth, sync, or remote APIs."""
    engine = build_engine(settings)
    sessions = lambda: session_scope(engine)
    gmail_profiles = LocalGmailProfileStore(settings.gmail_root)
    gmail_tokens = LocalGmailTokenStore(settings.gmail_root)
    gmail_state = LocalGmailStateStore(settings.gmail_root)
    gmail_oauth = GmailOAuth(settings.google_oauth_client_secrets, gmail_tokens)
    telegram_tokens = PrivateBotTokenStore(settings.effective_telegram_token_file)
    telegram_state = lambda: SqliteTelegramState(settings.telegram_state_file)
    telegram_status = lambda profile_id: _local_telegram_status(
        telegram_tokens, telegram_state, profile_id
    )
    drive_profiles = LocalProfileStore(settings.google_drive_root)
    drive_tokens = LocalTokenStore(settings.google_drive_root)
    drive_state = LocalSyncStateStore(settings.google_drive_root)
    sheets_profiles = LocalSheetsProfileStore(settings.google_sheets_root)

    def sheets_destination(profile_id: UUID) -> tuple[PanelDestination, ...]:
        profile_key = str(profile_id)
        if not sheets_profiles.exists(profile_key):
            return (
                PanelDestination(
                    "google_sheets",
                    "Google Таблица",
                    None,
                    "Появится после подключения Google Таблицы",
                ),
            )
        profile = sheets_profiles.load(profile_key)
        return (
            PanelDestination(
                "google_sheets",
                "Google Таблица",
                profile.spreadsheet_url,
                "Таблица создастся при первой синхронизации",
            ),
        )

    return PanelService(
        SqlAlchemyProfileRepository(sessions),
        (
            WhoopStatusReader(sessions, TokenStore(settings.whoop_token_root)),
            GmailStatusReader(gmail_profiles, gmail_state, gmail_oauth.local_status),
            TelegramStatusReader(telegram_status),
            ReminderStatusReader(sessions),
            DatabaseStatusReader(sessions),
        ),
        drive=DriveConfiguration(drive_profiles, drive_tokens, drive_state),
        destinations=(
            PanelDestination("metabase", "Дашборды", settings.metabase_url),
        ),
        destination_factory=sheets_destination,
    )


def _summary(profile: Profile) -> ProfileSummary:
    return ProfileSummary(id=profile.id, name=profile.name)


def _profile_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Profile name must be text")
    normalized = name.strip()
    if not normalized or len(normalized) > 255:
        raise ValueError("Profile name must contain 1 to 255 characters")
    return normalized


def _whoop_card(
    statuses: tuple[WhoopStatus, ...], account_ids: tuple[str, ...] = ()
) -> ConnectorCard:
    last_success = max(
        (status.last_success_at for status in statuses if status.last_success_at),
        default=None,
    )
    error_code = _panel_error_code(
        "whoop",
        next(
            (status.last_error_code for status in statuses if status.last_error_code),
            None,
        ),
    )
    if all(status.token_status == "ready" for status in statuses):
        result = "ready"
    elif any(status.auth_status == "reauth_required" for status in statuses):
        result = "reauth_required"
    else:
        result = "configured"
    return ConnectorCard(
        "whoop",
        result,
        f"Аккаунтов WHOOP в профиле: {len(statuses)}.",
        last_success,
        error_code,
        account_ids,
    )


def _gmail_card(
    profile: GmailProfile,
    state: LocalGmailStateStore,
    oauth_status: Callable[[str, str], str],
) -> ConnectorCard:
    if not profile.accounts:
        return ConnectorCard(
            "gmail",
            "not_configured",
            "Для этого профиля не настроен ни один аккаунт Gmail.",
            None,
            None,
        )
    statuses = tuple(
        oauth_status(profile.profile_id, account.account_id)
        for account in profile.accounts
    )
    runs = tuple(
        state.get_run_state(profile.profile_id, account.account_id)
        for account in profile.accounts
    )
    successes = tuple(
        parsed
        for run in runs
        if run.last_success_at
        if (parsed := _parse_datetime(run.last_success_at)) is not None
    )
    last_success = max(successes, default=None)
    error_code = _panel_error_code(
        "gmail",
        next((run.last_error_code for run in runs if run.last_error_code), None),
    )
    if any(status in {"invalid", "reauth_required"} for status in statuses):
        result = "reauth_required"
    elif all(status == "missing" for status in statuses):
        result = "needs_authorization"
    else:
        result = "ready"
    return ConnectorCard(
        "gmail",
        result,
        f"Аккаунтов Gmail в профиле: {len(profile.accounts)}.",
        last_success,
        error_code,
        tuple(account.account_id for account in profile.accounts),
    )


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _panel_error_code(connector: str, value: str | None) -> str | None:
    """Map persisted connector values to a closed, display-safe error set."""
    if value is None:
        return None
    allowed = {
        "whoop": _WHOOP_ERROR_CODES,
        "gmail": _GMAIL_ERROR_CODES,
        "telegram": _TELEGRAM_ERROR_CODES,
    }[connector]
    return value if value in allowed else f"{connector}_status_error"


def _drive_card() -> ConnectorCard:
    return ConnectorCard(
        "drive",
        "not_available",
        "Google Drive не интегрирован в этой установке.",
        None,
        None,
    )


def _local_telegram_status(
    tokens: PrivateBotTokenStore,
    state_factory: Callable[[], SqliteTelegramState],
    profile_id: UUID,
) -> TelegramStatus:
    """Read only persisted Telegram state; never verify it through the network."""
    if not tokens.exists():
        return TelegramStatus(
            token_configured=False,
            credential_verified=False,
            bot_id=None,
            bot_username=None,
            webhook_configured=None,
            poller_running=False,
            delivery_unknown_count=0,
            profile_id=profile_id,
            identity_bound=False,
            next_offset=None,
            last_poll_at=None,
            last_error_code="token_not_configured",
        )
    try:
        credential = tokens.load_verified()
    except (OSError, ValueError):
        return TelegramStatus(
            token_configured=True,
            credential_verified=False,
            bot_id=None,
            bot_username=None,
            webhook_configured=None,
            poller_running=False,
            delivery_unknown_count=0,
            profile_id=profile_id,
            identity_bound=False,
            next_offset=None,
            last_poll_at=None,
            last_error_code="credential_invalid",
        )
    state = state_factory()
    return TelegramStatus(
        token_configured=True,
        credential_verified=True,
        bot_id=credential.bot_id,
        bot_username=credential.username,
        webhook_configured=None,
        poller_running=False,
        delivery_unknown_count=state.delivery_unknown_count(
            credential.bot_id, profile_id
        ),
        profile_id=profile_id,
        identity_bound=(
            state.identity_for_profile(credential.bot_id, profile_id) is not None
        ),
        # Runtime state belongs to the bot, not to this profile. Do not present
        # it through a profile card.
        next_offset=None,
        last_poll_at=None,
        last_error_code=None,
    )
