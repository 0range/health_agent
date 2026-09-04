"""Autonomous, profile-safe Gmail medical attachment synchronization."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from dataclasses import dataclass

from health_agent.gmail.api import GmailItemUnavailable, HistoryCursorExpired
from health_agent.gmail.classifier import Classification, classify_attachment
from health_agent.gmail.config import GmailAccount, normalize_profile_id
from health_agent.gmail.types import (
    AttachmentImporter,
    AttachmentProvenance,
    GmailGateway,
    GmailMessage,
    GmailPart,
    GmailStateStore,
    GmailSyncReport,
    SeenAttachment,
    SeenMessage,
    walk_parts,
)


class GmailAccountMismatch(RuntimeError):
    """The account token does not match the configured profile/account binding."""


class InvalidAttachmentEncoding(ValueError):
    """Gmail returned malformed base64url attachment content."""


@dataclass(frozen=True, slots=True)
class GmailStatus:
    profile_id: str
    account_id: str
    account_email: str | None
    has_cursor: bool
    messages: int
    imported: int
    ambiguous: int


@dataclass(slots=True)
class _Stats:
    messages_seen: int = 0
    attachments_imported: int = 0
    ambiguous: int = 0
    ignored: int = 0
    unchanged: int = 0
    removed: int = 0

    def report(self, profile_id: str, account_id: str, mode: str) -> GmailSyncReport:
        return GmailSyncReport(
            profile_id=profile_id,
            account_id=account_id,
            mode=mode,
            messages_seen=self.messages_seen,
            attachments_imported=self.attachments_imported,
            ambiguous=self.ambiguous,
            ignored=self.ignored,
            unchanged=self.unchanged,
            removed=self.removed,
        )


class GmailService:
    """Reusable orchestration service for CLI and a future localhost panel."""

    def __init__(
        self,
        profile_id: str,
        account: GmailAccount,
        gateway: GmailGateway,
        state: GmailStateStore,
        importer: AttachmentImporter,
    ) -> None:
        self.profile_id = normalize_profile_id(profile_id)
        self.account = account
        self.gateway = gateway
        self.state = state
        self.importer = importer

    def verify_account(self) -> tuple[str, str]:
        profile = self.gateway.get_profile()
        actual = profile.email.casefold()
        if self.account.email is not None and actual != self.account.email.casefold():
            raise GmailAccountMismatch(
                f"Gmail account {self.account.account_id!r} is bound to another address"
            )
        return actual, profile.history_id

    def status(self) -> GmailStatus:
        messages, imported, ambiguous = self.state.counts(
            self.profile_id, self.account.account_id
        )
        return GmailStatus(
            profile_id=self.profile_id,
            account_id=self.account.account_id,
            account_email=self.account.email,
            has_cursor=(
                self.state.get_cursor(self.profile_id, self.account.account_id)
                is not None
            ),
            messages=messages,
            imported=imported,
            ambiguous=ambiguous,
        )

    def sync(self, *, full: bool = False) -> GmailSyncReport:
        account_email, current_history_id = self.verify_account()
        cursor = self.state.get_cursor(self.profile_id, self.account.account_id)
        if full or cursor is None:
            return self._full_sync(account_email, current_history_id, mode="full")
        try:
            return self._incremental_sync(account_email, cursor)
        except HistoryCursorExpired:
            # Gmail documents that history can disappear in less than a week.
            fresh_profile = self.gateway.get_profile()
            if fresh_profile.email.casefold() != account_email:
                raise GmailAccountMismatch("Gmail account changed during cursor recovery")
            return self._full_sync(
                account_email, fresh_profile.history_id, mode="recovery"
            )

    def _full_sync(
        self, account_email: str, target_history_id: str, *, mode: str
    ) -> GmailSyncReport:
        stats = _Stats()
        query = f"newer_than:{self.account.initial_lookback_days}d has:attachment"
        page_token: str | None = None
        processed: set[str] = set()
        while True:
            page = self.gateway.list_messages(query, page_token)
            for message_id in page.message_ids:
                if message_id not in processed:
                    self._process_message(message_id, account_email, stats)
                    processed.add(message_id)
            if page.next_page_token is None:
                break
            page_token = page.next_page_token
        self.state.set_cursor(self.profile_id, self.account.account_id, target_history_id)
        return stats.report(self.profile_id, self.account.account_id, mode)

    def _incremental_sync(
        self, account_email: str, cursor: str
    ) -> GmailSyncReport:
        stats = _Stats()
        page_token: str | None = None
        added: dict[str, None] = {}
        removed: dict[str, None] = {}
        target_history_id: str | None = None
        while True:
            page = self.gateway.list_history(cursor, page_token)
            for message_id in page.added_message_ids:
                added[message_id] = None
            for message_id in page.removed_message_ids:
                removed[message_id] = None
            target_history_id = page.history_id
            if page.next_page_token is None:
                break
            page_token = page.next_page_token
        for message_id in added:
            if message_id not in removed:
                self._process_message(message_id, account_email, stats)
        for message_id in removed:
            stats.removed += self.state.mark_message_removed(
                self.profile_id, self.account.account_id, message_id
            )
        if target_history_id is None:
            raise RuntimeError("Gmail history ended without a current history ID")
        self.state.set_cursor(
            self.profile_id, self.account.account_id, target_history_id
        )
        return stats.report(self.profile_id, self.account.account_id, "incremental")

    def _process_message(
        self, message_id: str, account_email: str, stats: _Stats
    ) -> None:
        try:
            message = self.gateway.get_message(message_id)
        except GmailItemUnavailable:
            stats.removed += self.state.mark_message_removed(
                self.profile_id, self.account.account_id, message_id
            )
            return
        stats.messages_seen += 1
        decisions: list[str] = []
        for part in walk_parts(message.payload):
            if not _is_attachment(part):
                continue
            classification = classify_attachment(
                message, part, self.account.trusted_senders
            )
            decisions.append(classification.decision)
            self._process_attachment(
                message, part, classification, account_email, stats
            )
        overall = _overall_classification(decisions)
        self.state.record_message(
            SeenMessage(
                profile_id=self.profile_id,
                account_id=self.account.account_id,
                message_id=message.message_id,
                history_id=message.history_id,
                internal_date_ms=message.internal_date_ms,
                classification=overall,
                status="processed",
            )
        )

    def _process_attachment(
        self,
        message: GmailMessage,
        part: GmailPart,
        classification: Classification,
        account_email: str,
        stats: _Stats,
    ) -> None:
        provenance = AttachmentProvenance(
            profile_id=self.profile_id,
            account_id=self.account.account_id,
            account_email=account_email,
            message_id=message.message_id,
            thread_id=message.thread_id,
            message_history_id=message.history_id,
            internal_date_ms=message.internal_date_ms,
            part_id=part.part_id,
            attachment_id=part.attachment_id,
            filename=part.filename,
            source_mime_type=classification.effective_mime_type or part.mime_type,
            classification=classification.decision,
            source_uri=f"https://mail.google.com/mail/#all/{message.message_id}",
        )
        previous = self.state.get_attachment(
            self.profile_id,
            self.account.account_id,
            message.message_id,
            part.part_id,
        )
        if previous is not None and previous.revision == provenance.revision:
            if previous.status == "imported":
                stats.unchanged += 1
                return
            if previous.classification == classification.decision:
                stats.unchanged += 1
                if previous.status == "ambiguous":
                    stats.ambiguous += 1
                elif previous.status == "ignored":
                    stats.ignored += 1
                return

        if classification.decision == "ignored":
            stats.ignored += 1
            self._record_attachment(provenance, part, "ignored")
            return
        if classification.decision == "ambiguous":
            stats.ambiguous += 1
            self._record_attachment(provenance, part, "ambiguous")
            return

        encoded = (
            part.body_data
            if part.body_data is not None
            else self._external_attachment(message.message_id, part.attachment_id)
        )
        if encoded is None:
            stats.removed += 1
            self._record_attachment(provenance, part, "unavailable")
            return
        try:
            receipt = self.importer.import_attachment(
                provenance, iter_base64url_chunks(encoded)
            )
        except InvalidAttachmentEncoding:
            stats.ambiguous += 1
            self._record_attachment(provenance, part, "invalid_encoding")
            return
        if part.body_size is not None and receipt.size_bytes != part.body_size:
            raise RuntimeError(
                f"Gmail attachment size mismatch for message {message.message_id!r}, "
                f"part {part.part_id!r}"
            )
        self.state.record_attachment(
            SeenAttachment(
                profile_id=self.profile_id,
                account_id=self.account.account_id,
                message_id=message.message_id,
                part_id=part.part_id,
                revision=provenance.revision,
                attachment_id=part.attachment_id,
                filename=part.filename,
                mime_type=provenance.source_mime_type,
                classification=classification.decision,
                declared_size_bytes=part.body_size,
                sha256=receipt.sha256,
                size_bytes=receipt.size_bytes,
                storage_reference=receipt.storage_reference,
                status="imported",
            )
        )
        stats.attachments_imported += 1

    def _external_attachment(
        self, message_id: str, attachment_id: str | None
    ) -> str | None:
        if attachment_id is None:
            return None
        try:
            return self.gateway.attachment_data(message_id, attachment_id)
        except GmailItemUnavailable:
            return None

    def _record_attachment(
        self, provenance: AttachmentProvenance, part: GmailPart, status: str
    ) -> None:
        self.state.record_attachment(
            SeenAttachment(
                profile_id=self.profile_id,
                account_id=self.account.account_id,
                message_id=provenance.message_id,
                part_id=provenance.part_id,
                revision=provenance.revision,
                attachment_id=part.attachment_id,
                filename="" if status == "ignored" else part.filename,
                mime_type=provenance.source_mime_type,
                classification=provenance.classification,
                declared_size_bytes=part.body_size,
                sha256=None,
                size_bytes=None,
                storage_reference=None,
                status=status,
            )
        )


def iter_base64url_chunks(
    data: str, encoded_chunk_size: int = 65536
) -> Iterator[bytes]:
    """Incrementally decode Gmail's unpadded base64url string."""
    if encoded_chunk_size < 4:
        raise ValueError("encoded chunk size must be at least four")
    pending = ""
    try:
        data.encode("ascii")
    except UnicodeEncodeError as error:
        raise InvalidAttachmentEncoding("attachment data is not ASCII base64url") from error
    for offset in range(0, len(data), encoded_chunk_size):
        pending += data[offset : offset + encoded_chunk_size]
        decodable = len(pending) - (len(pending) % 4)
        if decodable:
            block, pending = pending[:decodable], pending[decodable:]
            try:
                yield base64.b64decode(block, altchars=b"-_", validate=True)
            except (binascii.Error, ValueError) as error:
                raise InvalidAttachmentEncoding("invalid Gmail base64url data") from error
    if pending:
        padded = pending + "=" * (-len(pending) % 4)
        try:
            yield base64.b64decode(padded, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as error:
            raise InvalidAttachmentEncoding("invalid Gmail base64url data") from error


def _is_attachment(part: GmailPart) -> bool:
    return bool(part.filename or part.attachment_id)


def _overall_classification(decisions: list[str]) -> str:
    if "suspected_medical" in decisions:
        return "suspected_medical"
    if "ambiguous" in decisions:
        return "ambiguous"
    return "ignored"
