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
from health_agent.models import Profile
from health_agent.panel.models import ConnectorCard, ProfilePanel, ProfileSummary
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import TelegramStatus
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.status import WhoopStatus, get_whoop_status
from health_agent.whoop.tokens import TokenStore

SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


class ProfileRepository(Protocol):
    def list(self) -> tuple[ProfileSummary, ...]: ...

    def get(self, profile_id: UUID) -> ProfileSummary | None: ...

    def create(self, name: str) -> ProfileSummary: ...


class ConnectorStatusReader(Protocol):
    connector: str

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]: ...


class ProfileNotFoundError(LookupError):
    """The requested profile is not present in the local database."""


class SqlAlchemyProfileRepository:
    """Small database adapter that exposes only panel-safe profile fields."""

    def __init__(self, sessions: SessionScopeFactory) -> None:
        self._sessions = sessions

    def list(self) -> tuple[ProfileSummary, ...]:
        with self._sessions() as session:
            profiles = session.scalars(
                select(Profile).order_by(Profile.created_at, Profile.id)
            ).all()
        return tuple(_summary(profile) for profile in profiles)

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
                        "No WHOOP account is connected for this profile.",
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
        return (_whoop_card(statuses),)


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
                    "No Gmail account is configured for this profile.",
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
                    "Telegram is not configured locally.",
                    None,
                    status.last_error_code,
                ),
            )
        if not status.credential_verified:
            return (
                ConnectorCard(
                    self.connector,
                    "credential_invalid",
                    "Telegram credentials need local verification.",
                    None,
                    status.last_error_code,
                ),
            )
        if not status.identity_bound:
            return (
                ConnectorCard(
                    self.connector,
                    "not_bound",
                    "No Telegram identity is bound to this profile.",
                    status.last_poll_at,
                    status.last_error_code,
                ),
            )
        return (
            ConnectorCard(
                self.connector,
                "ready" if status.poller_running else "configured",
                (
                    "Telegram polling is active for this profile."
                    if status.poller_running
                    else "Telegram is configured for this profile."
                ),
                status.last_poll_at,
                status.last_error_code,
            ),
        )


class PanelService:
    """Build safe, isolated profile panels from injected repositories/readers."""

    def __init__(
        self,
        profiles: ProfileRepository,
        readers: tuple[ConnectorStatusReader, ...] | list[ConnectorStatusReader] = (),
    ) -> None:
        self._profiles = profiles
        self._readers = tuple(readers)

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
        return ProfilePanel(
            profile=profile,
            connectors=(*cards, _drive_card()),
        )

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
                    detail="Local connector status is unavailable.",
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
    telegram_state = SqliteTelegramState(settings.telegram_state_file)
    telegram_status = lambda profile_id: _local_telegram_status(
        telegram_tokens, telegram_state, profile_id
    )
    return PanelService(
        SqlAlchemyProfileRepository(sessions),
        (
            WhoopStatusReader(sessions, TokenStore(settings.whoop_token_root)),
            GmailStatusReader(gmail_profiles, gmail_state, gmail_oauth.local_status),
            TelegramStatusReader(telegram_status),
        ),
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


def _whoop_card(statuses: tuple[WhoopStatus, ...]) -> ConnectorCard:
    last_success = max(
        (status.last_success_at for status in statuses if status.last_success_at),
        default=None,
    )
    error_code = next(
        (status.last_error_code for status in statuses if status.last_error_code), None
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
        f"{len(statuses)} WHOOP account(s) configured for this profile.",
        last_success,
        error_code,
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
            "No Gmail account is configured for this profile.",
            None,
            None,
        )
    statuses = tuple(
        oauth_status(profile.profile_id, account.account_id) for account in profile.accounts
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
    error_code = next((run.last_error_code for run in runs if run.last_error_code), None)
    if any(status in {"invalid", "reauth_required"} for status in statuses):
        result = "reauth_required"
    elif all(status == "missing" for status in statuses):
        result = "needs_authorization"
    else:
        result = "ready"
    return ConnectorCard(
        "gmail",
        result,
        f"{len(profile.accounts)} Gmail account(s) configured for this profile.",
        last_success,
        error_code,
    )


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _drive_card() -> ConnectorCard:
    return ConnectorCard(
        "drive",
        "not_available",
        "Google Drive is not integrated in this installation.",
        None,
        None,
    )


def _local_telegram_status(
    tokens: PrivateBotTokenStore,
    state: SqliteTelegramState,
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
    next_offset, last_poll_at, last_error_code = state.runtime_status(credential.bot_id)
    return TelegramStatus(
        token_configured=True,
        credential_verified=True,
        bot_id=credential.bot_id,
        bot_username=credential.username,
        webhook_configured=None,
        poller_running=False,
        delivery_unknown_count=state.delivery_unknown_count(credential.bot_id, profile_id),
        profile_id=profile_id,
        identity_bound=(
            state.identity_for_profile(credential.bot_id, profile_id) is not None
        ),
        next_offset=next_offset,
        last_poll_at=last_poll_at,
        last_error_code=last_error_code,
    )
