"""Composable explicit Telegram actions with exact delivery-reply replay."""

from collections.abc import Sequence

from health_agent.questions.replies import PrivateReplyStore
from health_agent.telegram.types import MessageContext, TelegramTextActionService


class CompositeTelegramTextActions:
    def __init__(self, handlers: Sequence[TelegramTextActionService]) -> None:
        self._handlers = tuple(handlers)

    def handle(self, context: MessageContext, text: str) -> str | None:
        for handler in self._handlers:
            reply = handler.handle(context, text)
            if reply is not None:
                return reply
        return None


class PreparedTelegramTextActions:
    """Reuse the shared scoped spool; command handlers own mutation idempotency."""

    def __init__(
        self, handler: TelegramTextActionService, reply_store: PrivateReplyStore
    ) -> None:
        self._handler = handler
        self._reply_store = reply_store

    def handle(self, context: MessageContext, text: str) -> str | None:
        self._reply_store.sweep()
        prepared = self._reply_store.get(context)
        if prepared is not None:
            return prepared
        reply = self._handler.handle(context, text)
        return None if reply is None else self._reply_store.put(context, reply)
