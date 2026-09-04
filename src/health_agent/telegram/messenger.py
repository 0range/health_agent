"""Bot/profile-scoped outbound delivery with explicit unknown outcomes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from health_agent.telegram.api import (
    TelegramAPIError,
    TelegramDeferred,
    TelegramDeliveryUnknown,
)
from health_agent.telegram.types import TelegramGateway, TelegramState

MAX_MESSAGE_CHARACTERS = 4096


class OutboundDeliveryConflict(TelegramAPIError):
    def __init__(self) -> None:
        super().__init__("outbound_idempotency_conflict")


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    parts: int
    sent: int
    previously_sent: int


class TelegramMessenger:
    def __init__(
        self, bot_id: int, gateway: TelegramGateway, state: TelegramState
    ) -> None:
        self.bot_id = bot_id
        self.gateway = gateway
        self.state = state

    def send_to_profile(
        self, profile_id: UUID, text: str, *, delivery_key: str
    ) -> DeliveryReport:
        identity = self.state.identity_for_profile(self.bot_id, profile_id)
        if identity is None:
            raise ValueError("Telegram identity is not configured for this bot/profile")
        return self.send_to_chat(
            profile_id, identity.private_chat_id, text, delivery_key=delivery_key
        )

    def send_to_chat(
        self, profile_id: UUID, chat_id: int, text: str, *, delivery_key: str
    ) -> DeliveryReport:
        parts = split_message(text)
        sent = 0
        skipped = 0
        for index, part in enumerate(parts):
            digest = hashlib.sha256(part.encode("utf-8")).hexdigest()
            reservation = self.state.reserve_outbound(
                bot_id=self.bot_id,
                delivery_key=delivery_key,
                part_index=index,
                profile_id=profile_id,
                chat_id=chat_id,
                content_sha256=digest,
            )
            if reservation.status == "duplicate":
                skipped += 1
                continue
            if reservation.status == "deferred":
                assert reservation.next_retry_at is not None
                raise TelegramDeferred(reservation.next_retry_at)
            if reservation.status == "unknown":
                raise TelegramDeliveryUnknown()
            if reservation.status == "conflict":
                raise OutboundDeliveryConflict()
            try:
                message_id = self.gateway.send_message(chat_id, part)
            except TelegramDeferred as error:
                self.state.mark_outbound_failed(
                    self.bot_id,
                    profile_id,
                    delivery_key,
                    index,
                    error.safe_error_code,
                    status="deferred",
                    next_retry_at=error.retry_at,
                )
                raise
            except TelegramDeliveryUnknown as error:
                self.state.mark_outbound_failed(
                    self.bot_id,
                    profile_id,
                    delivery_key,
                    index,
                    error.safe_error_code,
                    status="unknown",
                )
                raise
            except TelegramAPIError as error:
                self.state.mark_outbound_failed(
                    self.bot_id,
                    profile_id,
                    delivery_key,
                    index,
                    error.safe_error_code,
                    status="failed",
                )
                raise
            if not self.state.mark_outbound_sent(
                self.bot_id,
                profile_id,
                delivery_key,
                index,
                message_id,
            ):
                self.state.mark_outbound_failed(
                    self.bot_id,
                    profile_id,
                    delivery_key,
                    index,
                    "delivery_acknowledgement_lost",
                    status="unknown",
                )
                raise TelegramDeliveryUnknown("delivery_acknowledgement_lost")
            sent += 1
        return DeliveryReport(len(parts), sent, skipped)


def split_message(text: str) -> tuple[str, ...]:
    text = text.strip()
    if not text:
        raise ValueError("Telegram message must not be empty")
    parts: list[str] = []
    remaining = text
    while len(remaining) > MAX_MESSAGE_CHARACTERS:
        candidate = remaining[:MAX_MESSAGE_CHARACTERS]
        boundary = max(candidate.rfind("\n"), candidate.rfind(" "))
        if boundary < MAX_MESSAGE_CHARACTERS // 2:
            boundary = MAX_MESSAGE_CHARACTERS
        part = remaining[:boundary].rstrip()
        parts.append(part)
        remaining = remaining[boundary:].lstrip()
    if remaining:
        parts.append(remaining)
    return tuple(parts)
