"""Idempotent-at-application-boundary Telegram reply and reminder delivery."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from health_agent.telegram.api import TelegramAPIError
from health_agent.telegram.types import TelegramGateway, TelegramState

MAX_MESSAGE_CHARACTERS = 4096


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    parts: int
    sent: int
    previously_reserved: int


class TelegramMessenger:
    def __init__(self, gateway: TelegramGateway, state: TelegramState) -> None:
        self.gateway = gateway
        self.state = state

    def send_to_profile(
        self, profile_id: UUID, text: str, *, delivery_key: str
    ) -> DeliveryReport:
        identity = self.state.identity_for_profile(profile_id)
        if identity is None:
            raise ValueError("Telegram identity is not configured for this profile")
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
            reserved = self.state.reserve_outbound(
                delivery_key=delivery_key,
                part_index=index,
                profile_id=profile_id,
                chat_id=chat_id,
            )
            if not reserved:
                skipped += 1
                continue
            try:
                message_id = self.gateway.send_message(chat_id, part)
            except TelegramAPIError as error:
                self.state.mark_outbound_failed(
                    delivery_key, index, error.safe_error_code
                )
                raise
            self.state.mark_outbound_sent(delivery_key, index, message_id)
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
