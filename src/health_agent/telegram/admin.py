"""Service-friendly, remotely verified Telegram administration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.models import Profile
from health_agent.telegram.api import MAX_SAFE_INTEGER, TelegramAPIError, TelegramBotAPI
from health_agent.telegram.stores import (
    PrivateBotTokenStore,
    SqliteTelegramState,
    TelegramIdentityConflict,
)
from health_agent.telegram.types import (
    ProfileDirectory,
    TelegramGateway,
    TelegramIdentity,
    TelegramStatus,
    VerifiedBotCredential,
)


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
        *,
        gateway_factory: Callable[[str], TelegramGateway] = TelegramBotAPI,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat_fresh_for: timedelta = timedelta(minutes=2),
    ) -> None:
        self.tokens = tokens
        self.state = state
        self.profiles = profiles
        self.gateway_factory = gateway_factory
        self.clock = clock
        self.heartbeat_fresh_for = heartbeat_fresh_for

    def configure_token(self, token: str) -> VerifiedBotCredential:
        gateway = self.gateway_factory(token)
        credential = _verified_credential(token, gateway.get_me())
        # The credential file atomically publishes token+bot_id. A namespace can
        # safely exist before publication; it never changes another bot's offset.
        self.state.register_bot(credential.bot_id, credential.username)
        self.tokens.save_verified(credential)
        return credential

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
        credential = self.tokens.load_verified()
        return self.state.bind_identity(
            credential.bot_id, TelegramIdentity(telegram_user_id, profile_id, chat_id)
        )

    def unbind_identity(self, profile_id: UUID) -> bool:
        credential = self.tokens.load_verified()
        return self.state.unbind_identity(credential.bot_id, profile_id)

    def status(self, profile_id: UUID | None = None) -> TelegramStatus:
        configured = self.tokens.exists()
        if not configured:
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
            credential = self.tokens.load_verified()
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
        offset, last_poll, stored_error = self.state.runtime_status(credential.bot_id)
        identity = (
            None
            if profile_id is None
            else self.state.identity_for_profile(credential.bot_id, profile_id)
        )
        unknown_count = self.state.delivery_unknown_count(credential.bot_id, profile_id)
        verified = False
        webhook: bool | None = None
        error = stored_error
        bot_username = credential.username
        try:
            gateway = self.gateway_factory(credential.token)
            remote = _verified_credential(credential.token, gateway.get_me())
            if remote.bot_id != credential.bot_id:
                error = "bot_identity_mismatch"
            else:
                verified = True
                bot_username = remote.username
                webhook = bool(gateway.get_webhook_url())
                if webhook:
                    error = "webhook_configured"
        except TelegramAPIError as api_error:
            error = api_error.safe_error_code
        now = self.clock().astimezone(UTC)
        poller_running = (
            verified
            and webhook is False
            and last_poll is not None
            and now - last_poll.astimezone(UTC) <= self.heartbeat_fresh_for
        )
        return TelegramStatus(
            token_configured=True,
            credential_verified=verified,
            bot_id=credential.bot_id,
            bot_username=bot_username,
            webhook_configured=webhook,
            poller_running=poller_running,
            delivery_unknown_count=unknown_count,
            profile_id=profile_id,
            identity_bound=identity is not None,
            next_offset=offset,
            last_poll_at=last_poll,
            last_error_code=error,
        )


def _verified_credential(
    token: str, result: dict[str, object]
) -> VerifiedBotCredential:
    bot_id = result.get("id")
    is_bot = result.get("is_bot")
    username = result.get("username")
    if (
        isinstance(bot_id, bool)
        or not isinstance(bot_id, int)
        or bot_id <= 0
        or bot_id > MAX_SAFE_INTEGER
        or is_bot is not True
        or (username is not None and not isinstance(username, str))
    ):
        raise TelegramAPIError("invalid_get_me_response")
    return VerifiedBotCredential(token, bot_id, username)


__all__ = [
    "DatabaseProfileDirectory",
    "TelegramAdminService",
    "TelegramIdentityConflict",
]
