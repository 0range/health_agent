"""Service-friendly Telegram administration for CLI and a future local panel."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.models import Profile
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import (
    ProfileDirectory,
    TelegramIdentity,
    TelegramStatus,
)


class TelegramIdentityConflict(ValueError):
    """Raised when a user or profile is already bound to another identity."""


class DatabaseProfileDirectory:
    """Read-only profile lookup shared by CLI and a future local panel."""

    def __init__(self, settings: Settings) -> None:
        self._engine = build_engine(settings)

    def exists(self, profile_id: UUID) -> bool:
        with session_scope(self._engine) as session:
            return (
                session.scalar(select(Profile.id).where(Profile.id == profile_id))
                is not None
            )


class TelegramAdminService:
    def __init__(
        self,
        tokens: PrivateBotTokenStore,
        state: SqliteTelegramState,
        profiles: ProfileDirectory,
    ) -> None:
        self.tokens = tokens
        self.state = state
        self.profiles = profiles

    def configure_token(self, token: str) -> None:
        self.tokens.save(token)

    def bind_identity(
        self,
        profile_id: UUID,
        telegram_user_id: int,
        private_chat_id: int | None = None,
    ) -> TelegramIdentity:
        if not self.profiles.exists(profile_id):
            raise ValueError("profile does not exist")
        if telegram_user_id <= 0:
            raise ValueError("Telegram user ID must be positive")
        chat_id = telegram_user_id if private_chat_id is None else private_chat_id
        if chat_id <= 0:
            raise ValueError("Private chat ID must be positive")
        existing_user = self.state.identity_for_user(telegram_user_id)
        existing_profile = self.state.identity_for_profile(profile_id)
        if existing_user is not None and existing_user.profile_id != profile_id:
            raise TelegramIdentityConflict("Telegram identity is already bound")
        if (
            existing_profile is not None
            and existing_profile.telegram_user_id != telegram_user_id
        ):
            raise TelegramIdentityConflict("Telegram identity is already bound")
        identity = TelegramIdentity(telegram_user_id, profile_id, chat_id)
        self.state.bind_identity(identity)
        return identity

    def unbind_identity(self, profile_id: UUID) -> bool:
        return self.state.unbind_identity(profile_id)

    def status(self, profile_id: UUID | None = None) -> TelegramStatus:
        offset, last_poll, error = self.state.runtime_status()
        identity = (
            None if profile_id is None else self.state.identity_for_profile(profile_id)
        )
        return TelegramStatus(
            token_configured=self.tokens.exists(),
            profile_id=profile_id,
            identity_bound=identity is not None,
            next_offset=offset,
            last_poll_at=last_poll,
            last_error_code=error,
        )
