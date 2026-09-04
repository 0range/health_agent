"""Autonomous, serialized Gmail medical synchronization."""

from __future__ import annotations

from dataclasses import dataclass

from health_agent.gmail.api import GmailItemUnavailable, HistoryCursorExpired
from health_agent.gmail.classifier import (
    Classification,
    classify_attachment,
    classify_message,
)
from health_agent.gmail.config import GmailAccount, normalize_profile_id
from health_agent.gmail.preparation import (
    AttachmentPreparationError,
    SafeAttachmentPreparer,
)
from health_agent.gmail.types import (
    AttachmentImporter,
    AttachmentProvenance,
    EncodedBody,
    GmailGateway,
    GmailMessage,
    GmailPart,
    GmailRunState,
    GmailStateStore,
    GmailSyncReport,
    SeenAttachment,
    SeenMessage,
    walk_parts,
)

_EXCLUDED_LABELS = {"SPAM", "TRASH"}
_TERMINAL_OUTCOMES = {"medically_imported", "duplicate", "non_medical"}


class GmailAccountMismatch(RuntimeError):
    """The token does not match its verified account binding."""


class GmailPaginationLoop(RuntimeError):
    """Gmail repeated a page token and the scan was stopped."""


@dataclass(frozen=True, slots=True)
class GmailStatus:
    profile_id: str
    account_id: str
    account_email: str | None
    cursor: str | None
    counts: dict[str, int]
    run: GmailRunState


@dataclass(slots=True)
class _Stats:
    messages_seen: int = 0
    attachments_staged: int = 0
    medically_imported: int = 0
    duplicates: int = 0
    ocr_required: int = 0
    needs_attention: int = 0
    ignored: int = 0
    unchanged: int = 0
    removed: int = 0

    def report(self, profile_id: str, account_id: str, mode: str) -> GmailSyncReport:
        return GmailSyncReport(
            profile_id=profile_id,
            account_id=account_id,
            mode=mode,
            messages_seen=self.messages_seen,
            attachments_staged=self.attachments_staged,
            medically_imported=self.medically_imported,
            duplicates=self.duplicates,
            ocr_required=self.ocr_required,
            needs_attention=self.needs_attention,
            ignored=self.ignored,
            unchanged=self.unchanged,
            removed=self.removed,
        )


class GmailService:
    """Reusable orchestration service for CLI and the localhost panel."""

    def __init__(
        self,
        profile_id: str,
        account: GmailAccount,
        gateway: GmailGateway,
        state: GmailStateStore,
        importer: AttachmentImporter,
        preparer: SafeAttachmentPreparer,
    ) -> None:
        self.profile_id = normalize_profile_id(profile_id)
        self.account = account
        self.gateway = gateway
        self.state = state
        self.importer = importer
        self.preparer = preparer

    def verify_account(self) -> tuple[str, str]:
        profile = self.gateway.get_profile()
        actual = profile.email.casefold()
        if self.account.email is None or actual != self.account.email.casefold():
            raise GmailAccountMismatch(
                "Gmail token does not match the verified binding"
            )
        return actual, profile.history_id

    def status(self) -> GmailStatus:
        return GmailStatus(
            profile_id=self.profile_id,
            account_id=self.account.account_id,
            account_email=self.account.email,
            cursor=self.state.get_cursor(self.profile_id, self.account.account_id),
            counts=self.state.counts(self.profile_id, self.account.account_id),
            run=self.state.get_run_state(self.profile_id, self.account.account_id),
        )

    def sync(self, *, full: bool = False) -> GmailSyncReport:
        with self.state.sync_lock(self.profile_id, self.account.account_id):
            cursor = self.state.get_cursor(self.profile_id, self.account.account_id)
            mode = "full" if full or cursor is None else "incremental"
            self.state.begin_sync(self.profile_id, self.account.account_id, mode)
            try:
                account_email, current_history_id = self.verify_account()
                if mode == "full":
                    report = self._full_sync(
                        account_email, current_history_id, mode="full"
                    )
                else:
                    assert cursor is not None
                    try:
                        report = self._incremental_sync(account_email, cursor)
                    except HistoryCursorExpired:
                        fresh = self.gateway.get_profile()
                        if fresh.email.casefold() != account_email:
                            raise GmailAccountMismatch(
                                "Gmail account changed during cursor recovery"
                            )
                        self.state.begin_sync(
                            self.profile_id, self.account.account_id, "recovery"
                        )
                        report = self._full_sync(
                            account_email, fresh.history_id, mode="recovery"
                        )
            except Exception as error:
                self.state.fail_sync(
                    self.profile_id, self.account.account_id, _safe_error_code(error)
                )
                raise
            self.state.finish_sync(self.profile_id, self.account.account_id)
            return report

    def _full_sync(
        self, account_email: str, target_history_id: str, *, mode: str
    ) -> GmailSyncReport:
        stats = _Stats()
        query = f"newer_than:{self.account.initial_lookback_days}d -in:spam -in:trash"
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        processed: set[str] = set()
        while True:
            page = self.gateway.list_messages(query, page_token)
            for message_id in page.message_ids:
                if message_id not in processed:
                    self._process_message(message_id, account_email, stats)
                    processed.add(message_id)
            page_token = _next_page_token(page.next_page_token, seen_page_tokens)
            if page_token is None:
                break
        self.state.set_cursor(
            self.profile_id, self.account.account_id, target_history_id
        )
        return stats.report(self.profile_id, self.account.account_id, mode)

    def _incremental_sync(self, account_email: str, cursor: str) -> GmailSyncReport:
        stats = _Stats()
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        changed: dict[str, None] = {}
        deleted: dict[str, None] = {}
        target_history_id: str | None = None
        while True:
            page = self.gateway.list_history(cursor, page_token)
            for message_id in page.changed_message_ids:
                changed[message_id] = None
            for message_id in page.deleted_message_ids:
                deleted[message_id] = None
            target_history_id = page.history_id
            page_token = _next_page_token(page.next_page_token, seen_page_tokens)
            if page_token is None:
                break
        for message_id in changed:
            if message_id not in deleted:
                self._process_message(message_id, account_email, stats)
        for message_id in deleted:
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
        if _EXCLUDED_LABELS.intersection(message.label_ids):
            stats.removed += self.state.mark_message_removed(
                self.profile_id, self.account.account_id, message.message_id
            )
            self._record_message(message, "excluded", "excluded")
            return

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

        body_classification = classify_message(message)
        if body_classification.decision == "appointment":
            decisions.append("appointment")
            stats.needs_attention += 1
        overall = _overall_classification(decisions)
        self._record_message(
            message,
            overall,
            "attention" if overall == "appointment" else "processed",
        )

    def _record_message(
        self, message: GmailMessage, classification: str, status: str
    ) -> None:
        self.state.record_message(
            SeenMessage(
                profile_id=self.profile_id,
                account_id=self.account.account_id,
                message_id=message.message_id,
                history_id=message.history_id,
                internal_date_ms=message.internal_date_ms,
                classification=classification,
                status=status,
                label_ids=message.label_ids,
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
        provenance = _provenance(
            self.profile_id,
            self.account.account_id,
            account_email,
            message,
            part,
            classification,
        )
        previous = self.state.get_attachment(
            self.profile_id,
            self.account.account_id,
            message.message_id,
            part.part_id,
            provenance.revision,
        )
        if previous is not None and (
            previous.status != "removed"
            and (previous.outcome in _TERMINAL_OUTCOMES or previous.status == "ignored")
        ):
            stats.unchanged += 1
            return
        if classification.decision == "ignored":
            stats.ignored += 1
            self._record_attachment(provenance, part, "ignored", outcome="ignored")
            return

        try:
            self.preparer.validate_before_download(part.body_size)
            body = (
                EncodedBody(part.body_data, part.body_size)
                if part.body_data is not None
                else self._external_attachment(message.message_id, part.attachment_id)
            )
            if body is None:
                self._record_attention(provenance, part, "unavailable", stats)
                return
            if (
                part.body_size is not None
                and body.size_bytes is not None
                and body.size_bytes != part.body_size
            ):
                raise AttachmentPreparationError(
                    "Gmail attachment response size disagrees with message metadata"
                )
            with self.preparer.prepare(
                provenance, body.data, part.body_size or body.size_bytes
            ) as prepared:
                receipt = self.importer.import_attachment(provenance, prepared)
                if (
                    receipt.sha256 != prepared.sha256
                    or receipt.size_bytes != prepared.size_bytes
                ):
                    raise RuntimeError(
                        "attachment importer receipt does not match prepared bytes"
                    )
                detected_mime = prepared.detected_mime_type
        except AttachmentPreparationError as error:
            self._record_attention(provenance, part, _safe_error_code(error), stats)
            return

        self.state.record_attachment(
            SeenAttachment(
                profile_id=self.profile_id,
                account_id=self.account.account_id,
                message_id=message.message_id,
                part_id=part.part_id,
                revision=provenance.revision,
                attachment_id=part.attachment_id,
                filename=part.filename,
                mime_type=detected_mime,
                classification=classification.decision,
                declared_size_bytes=part.body_size,
                sha256=receipt.sha256,
                size_bytes=receipt.size_bytes,
                storage_reference=receipt.storage_reference,
                status=(
                    "attention"
                    if receipt.outcome in {"ocr_required", "needs_attention"}
                    else "processed"
                ),
                thread_id=provenance.thread_id,
                message_history_id=provenance.message_history_id,
                internal_date_ms=provenance.internal_date_ms,
                account_email=provenance.account_email,
                source_uri=provenance.source_uri,
                outcome=receipt.outcome,
                document_id=receipt.document_id,
            )
        )
        if receipt.storage_reference is not None:
            stats.attachments_staged += 1
        if receipt.outcome == "medically_imported":
            stats.medically_imported += 1
        elif receipt.outcome == "duplicate":
            stats.duplicates += 1
        elif receipt.outcome == "ocr_required":
            stats.ocr_required += 1
            stats.needs_attention += 1
        elif receipt.outcome == "needs_attention":
            stats.needs_attention += 1
        elif receipt.outcome == "non_medical":
            stats.ignored += 1

    def _record_attention(
        self,
        provenance: AttachmentProvenance,
        part: GmailPart,
        outcome: str,
        stats: _Stats,
    ) -> None:
        stats.needs_attention += 1
        self._record_attachment(provenance, part, "attention", outcome=outcome)

    def _external_attachment(
        self, message_id: str, attachment_id: str | None
    ) -> EncodedBody | None:
        if attachment_id is None:
            return None
        try:
            return self.gateway.attachment_data(message_id, attachment_id)
        except GmailItemUnavailable:
            return None

    def _record_attachment(
        self,
        provenance: AttachmentProvenance,
        part: GmailPart,
        status: str,
        *,
        outcome: str,
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
                thread_id=provenance.thread_id,
                message_history_id=provenance.message_history_id,
                internal_date_ms=provenance.internal_date_ms,
                account_email=provenance.account_email,
                source_uri=provenance.source_uri,
                outcome=outcome,
            )
        )


def _provenance(
    profile_id: str,
    account_id: str,
    account_email: str,
    message: GmailMessage,
    part: GmailPart,
    classification: Classification,
) -> AttachmentProvenance:
    return AttachmentProvenance(
        profile_id=profile_id,
        account_id=account_id,
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


def _next_page_token(value: str | None, seen: set[str]) -> str | None:
    if value is None:
        return None
    if value in seen:
        raise GmailPaginationLoop("Gmail repeated a pagination token")
    seen.add(value)
    return value


def _is_attachment(part: GmailPart) -> bool:
    return bool(part.filename or part.attachment_id)


def _overall_classification(decisions: list[str]) -> str:
    if "appointment" in decisions:
        return "appointment"
    if "suspected_medical" in decisions:
        return "suspected_medical"
    if "ambiguous" in decisions:
        return "ambiguous"
    return "ignored"


def _safe_error_code(error: BaseException) -> str:
    return type(error).__name__
