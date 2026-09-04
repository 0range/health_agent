from __future__ import annotations

import base64
from pathlib import Path

import pytest

from health_agent.gmail.api import GmailItemUnavailable, HistoryCursorExpired
from health_agent.gmail.config import GmailAccount
from health_agent.gmail.preparation import SafeAttachmentPreparer
from health_agent.gmail.service import GmailPaginationLoop, GmailService
from health_agent.gmail.stores import LocalGmailStateStore
from health_agent.gmail.types import (
    AttachmentProvenance,
    EncodedBody,
    GmailMessage,
    GmailPart,
    HistoryPage,
    ImportReceipt,
    MailboxProfile,
    MessageInboxReceipt,
    MessagePage,
    MessageProvenance,
    PreparedAttachment,
    SeenAttachment,
)

PROFILE = "11111111-1111-1111-1111-111111111111"
PDF = b"%PDF-1.4\n"


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class FakeGateway:
    def __init__(self) -> None:
        self.profiles = [MailboxProfile("alice@example.com", "100")]
        self.profile_calls = 0
        self.message_pages: dict[str | None, MessagePage] = {}
        self.history_pages: dict[str | None, HistoryPage] = {}
        self.messages: dict[str, GmailMessage] = {}
        self.attachments: dict[tuple[str, str], EncodedBody] = {}
        self.queries: list[str] = []
        self.attachment_calls = 0
        self.history_error: Exception | None = None
        self.deleted_messages: set[str] = set()

    def get_profile(self) -> MailboxProfile:
        value = self.profiles[min(self.profile_calls, len(self.profiles) - 1)]
        self.profile_calls += 1
        return value

    def list_messages(self, query: str, page_token: str | None) -> MessagePage:
        self.queries.append(query)
        return self.message_pages.get(page_token, MessagePage((), None))

    def get_message(self, message_id: str) -> GmailMessage:
        if message_id in self.deleted_messages:
            raise GmailItemUnavailable("gone")
        return self.messages[message_id]

    def list_history(self, history_id: str, page_token: str | None) -> HistoryPage:
        if self.history_error is not None:
            raise self.history_error
        return self.history_pages[page_token]

    def attachment_data(self, message_id: str, attachment_id: str) -> EncodedBody:
        self.attachment_calls += 1
        return self.attachments[(message_id, attachment_id)]


class FakeImporter:
    def __init__(
        self,
        outcome: str = "medically_imported",
        fail: bool = False,
        processing_status: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.fail = fail
        self.processing_status = processing_status
        self.calls: list[tuple[AttachmentProvenance, PreparedAttachment]] = []

    def import_attachment(
        self, provenance: AttachmentProvenance, prepared: PreparedAttachment
    ) -> ImportReceipt:
        if self.fail:
            raise RuntimeError("import failed")
        self.calls.append((provenance, prepared))
        return ImportReceipt(
            prepared.sha256,
            prepared.size_bytes,
            "document-id" if self.outcome != "non_medical" else None,
            self.outcome,
            document_id="document-id" if self.outcome != "non_medical" else None,
            processing_status=self.processing_status,
        )


class FakeMessageInbox:
    def __init__(self) -> None:
        self.calls: list[MessageProvenance] = []

    def queue_message(self, provenance: MessageProvenance) -> MessageInboxReceipt:
        self.calls.append(provenance)
        return MessageInboxReceipt("source-record-id", "queued")


class SequenceImporter(FakeImporter):
    def __init__(self) -> None:
        super().__init__()
        self.outcomes = iter(("medically_imported", "duplicate"))

    def import_attachment(
        self, provenance: AttachmentProvenance, prepared: PreparedAttachment
    ) -> ImportReceipt:
        self.outcome = next(self.outcomes)
        return super().import_attachment(provenance, prepared)


class FailFirstAttachmentState(LocalGmailStateStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.failed = False

    def record_attachment(self, attachment: SeenAttachment) -> None:
        if not self.failed:
            self.failed = True
            raise OSError("state write failed")
        super().record_attachment(attachment)


def attachment_message(
    message_id: str = "m1",
    *,
    attachment_id: str = "a1",
    labels: tuple[str, ...] = ("INBOX",),
    subject: str = "Medical laboratory",
) -> GmailMessage:
    return GmailMessage(
        message_id,
        "thread-1",
        "90",
        1_725_449_600_000,
        subject,
        "clinic@example.com",
        GmailPart(
            "",
            "multipart/mixed",
            "",
            None,
            None,
            children=(
                GmailPart("1", "application/pdf", "labs.pdf", attachment_id, len(PDF)),
            ),
        ),
        labels,
    )


def build_service(
    tmp_path: Path,
    gateway: FakeGateway,
    state: LocalGmailStateStore | None = None,
    importer: FakeImporter | None = None,
    message_inbox: FakeMessageInbox | None = None,
    *,
    max_bytes: int = 1024,
) -> tuple[GmailService, LocalGmailStateStore, FakeImporter]:
    state = state or LocalGmailStateStore(tmp_path / "state")
    importer = importer or FakeImporter()
    service = GmailService(
        PROFILE,
        GmailAccount.create("personal").with_email("alice@example.com"),
        gateway,
        state,
        importer,
        message_inbox or FakeMessageInbox(),
        SafeAttachmentPreparer(tmp_path / "tmp", max_bytes),
    )
    return service, state, importer


def test_full_scan_pages_and_reports_real_outcomes(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages = {
        None: MessagePage(("m1",), "next"),
        "next": MessagePage(("m1",), None),
    }
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, state, importer = build_service(tmp_path, gateway)

    first = service.sync()
    second = service.sync(full=True)

    assert gateway.queries[0] == "newer_than:7d -in:spam -in:trash"
    assert first.medically_imported == 1
    assert first.attachments_staged == 1
    assert second.unchanged == 1
    assert len(importer.calls) == 1
    assert state.get_cursor(PROFILE, "personal") == "100"
    assert state.get_run_state(PROFILE, "personal").last_success_at is not None


def test_body_only_appointment_is_retained_as_attention(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    body = GmailPart(
        "", "text/plain", "", None, 31, encoded("Приём у терапевта завтра".encode())
    )
    gateway.messages["m1"] = GmailMessage(
        "m1", "t1", "90", 1000, "Reminder", "clinic@example.com", body, ("INBOX",)
    )
    service, state, _ = build_service(tmp_path, gateway)

    report = service.sync()

    assert report.needs_attention == 1
    seen = state.get_message(PROFILE, "personal", "m1")
    assert seen is not None
    assert seen.classification == "appointment"
    assert seen.source_record_id == "source-record-id"
    assert state.attention_messages(PROFILE, "personal") == (seen,)


def test_body_only_medical_result_is_conservatively_queued(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    body = GmailPart(
        "", "text/plain", "", None, 20, encoded("Ваши анализы готовы".encode())
    )
    gateway.messages["m1"] = GmailMessage(
        "m1", "t1", "90", 1000, "Results", "clinic@example.com", body, ("INBOX",)
    )
    inbox = FakeMessageInbox()
    service, state, _ = build_service(tmp_path, gateway, message_inbox=inbox)

    report = service.sync()

    assert report.needs_attention == 1
    assert len(inbox.calls) == 1
    assert inbox.calls[0].classification == "body_medical"
    assert state.attention_messages(PROFILE, "personal")[0].message_id == "m1"


def test_arbitrary_body_is_not_persisted_or_queued(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    body = b"Quarterly planning notes"
    gateway.messages["m1"] = GmailMessage(
        "m1",
        "t1",
        "90",
        1000,
        "Notes",
        "sender@example.com",
        GmailPart("", "text/plain", "", None, len(body), encoded(body)),
        ("INBOX",),
    )
    inbox = FakeMessageInbox()
    service, state, _ = build_service(tmp_path, gateway, message_inbox=inbox)

    report = service.sync()

    assert report.needs_attention == 0
    assert inbox.calls == []
    assert state.get_message(PROFILE, "personal", "m1") is None


def test_incremental_spam_is_excluded_and_restored_message_is_processed(
    tmp_path: Path,
) -> None:
    state = LocalGmailStateStore(tmp_path / "state")
    state.set_cursor(PROFILE, "personal", "100")
    gateway = FakeGateway()
    gateway.history_pages[None] = HistoryPage(
        ("spam", "restored"), ("deleted",), None, "110"
    )
    gateway.messages["spam"] = attachment_message("spam", labels=("SPAM",))
    gateway.messages["restored"] = attachment_message("restored", labels=("INBOX",))
    gateway.attachments[("restored", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, _, importer = build_service(tmp_path, gateway, state)

    report = service.sync()

    assert report.medically_imported == 1
    assert len(importer.calls) == 1
    assert state.get_message(PROFILE, "personal", "spam").status == "excluded"  # type: ignore[union-attr]
    assert state.get_cursor(PROFILE, "personal") == "110"


def test_previously_imported_message_is_reactivated_after_spam_restore(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, state, importer = build_service(tmp_path, gateway)
    service.sync()

    gateway.history_pages[None] = HistoryPage(("m1",), (), None, "110")
    gateway.messages["m1"] = attachment_message(labels=("SPAM",))
    service.sync()
    assert (
        state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:a1").status
        == "removed"
    )  # type: ignore[union-attr]

    gateway.history_pages[None] = HistoryPage(("m1",), (), None, "120")
    gateway.messages["m1"] = attachment_message(labels=("INBOX",))
    service.sync()

    assert len(importer.calls) == 2
    assert (
        state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:a1").status
        == "processed"
    )  # type: ignore[union-attr]


def test_expired_history_recovers_with_lookback(tmp_path: Path) -> None:
    state = LocalGmailStateStore(tmp_path / "state")
    state.set_cursor(PROFILE, "personal", "old")
    gateway = FakeGateway()
    gateway.history_error = HistoryCursorExpired("expired")
    gateway.profiles = [
        MailboxProfile("alice@example.com", "200"),
        MailboxProfile("alice@example.com", "201"),
    ]
    gateway.message_pages[None] = MessagePage((), None)
    service, _, _ = build_service(tmp_path, gateway, state)

    report = service.sync()

    assert report.mode == "recovery"
    assert state.get_cursor(PROFILE, "personal") == "201"


def test_full_scan_reconciles_known_message_moved_to_trash(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, state, importer = build_service(tmp_path, gateway)
    service.sync()
    gateway.message_pages[None] = MessagePage((), None)
    gateway.messages["m1"] = attachment_message(labels=("TRASH",))

    report = service.sync(full=True)

    assert report.removed == 2
    assert state.get_message(PROFILE, "personal", "m1").status == "excluded"  # type: ignore[union-attr]
    assert (
        state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:a1").status
        == "removed"
    )  # type: ignore[union-attr]
    assert state.get_cursor(PROFILE, "personal") == "100"

    gateway.messages["m1"] = attachment_message(labels=("INBOX",))
    service.sync(full=True)
    assert len(importer.calls) == 2
    assert state.get_message(PROFILE, "personal", "m1").status == "processed"  # type: ignore[union-attr]


def test_expired_history_recovery_reconciles_known_deleted_message(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    gateway.profiles = [
        MailboxProfile("alice@example.com", "100"),
        MailboxProfile("alice@example.com", "201"),
    ]
    service, state, _ = build_service(tmp_path, gateway)
    service.sync()
    gateway.history_error = HistoryCursorExpired("expired")
    gateway.message_pages[None] = MessagePage((), None)
    gateway.deleted_messages.add("m1")

    report = service.sync()

    assert report.mode == "recovery"
    assert report.removed == 2
    assert state.get_message(PROFILE, "personal", "m1").status == "removed"  # type: ignore[union-attr]
    assert state.get_cursor(PROFILE, "personal") == "201"


def test_import_failure_preserves_cursor_and_records_safe_error(tmp_path: Path) -> None:
    state = LocalGmailStateStore(tmp_path / "state")
    state.set_cursor(PROFILE, "personal", "100")
    gateway = FakeGateway()
    gateway.history_pages[None] = HistoryPage(("m1",), (), None, "110")
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, _, _ = build_service(tmp_path, gateway, state, FakeImporter(fail=True))

    with pytest.raises(RuntimeError, match="import failed"):
        service.sync()

    assert state.get_cursor(PROFILE, "personal") == "100"
    assert state.get_run_state(PROFILE, "personal").last_error_code == "RuntimeError"


def test_repeated_page_token_stops_without_advancing_cursor(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages = {
        None: MessagePage((), "same"),
        "same": MessagePage((), "same"),
    }
    service, state, _ = build_service(tmp_path, gateway)

    with pytest.raises(GmailPaginationLoop):
        service.sync()
    assert state.get_cursor(PROFILE, "personal") is None


def test_declared_oversize_rejected_before_attachment_download(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    oversized = GmailPart("1", "application/pdf", "labs.pdf", "a1", 2048)
    gateway.messages["m1"] = GmailMessage(
        "m1",
        "thread-1",
        "90",
        1000,
        "Medical laboratory",
        "clinic@example.com",
        GmailPart("", "multipart/mixed", "", None, None, children=(oversized,)),
        ("INBOX",),
    )
    service, _, _ = build_service(tmp_path, gateway, max_bytes=1024)

    report = service.sync()

    assert report.needs_attention == 1
    assert gateway.attachment_calls == 0


def test_magic_mismatch_never_calls_importer(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(b"not-a-pdf"), len(PDF))
    service, _, importer = build_service(tmp_path, gateway)

    report = service.sync()

    assert report.needs_attention == 1
    assert importer.calls == []


def test_changed_attachment_id_retains_both_immutable_revisions(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message(attachment_id="a1")
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, state, _ = build_service(tmp_path, gateway)
    service.sync()
    gateway.messages["m1"] = attachment_message(attachment_id="a2")
    gateway.attachments[("m1", "a2")] = EncodedBody(encoded(PDF), len(PDF))

    service.sync(full=True)

    assert state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:a1") is not None
    assert state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:a2") is not None


def test_unnamed_supported_attachment_gets_safe_deterministic_name(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    message = attachment_message(subject="Files")
    unnamed = GmailPart("1", "application/pdf", "", "a1", len(PDF))
    gateway.messages["m1"] = GmailMessage(
        message.message_id,
        message.thread_id,
        message.history_id,
        message.internal_date_ms,
        message.subject,
        message.sender,
        GmailPart("", "multipart/mixed", "", None, None, children=(unnamed,)),
        message.label_ids,
    )
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    service, state, importer = build_service(tmp_path, gateway)

    service.sync()

    filename = importer.calls[0][0].filename
    assert filename.startswith("gmail-") and filename.endswith(".pdf")
    assert "/" not in filename
    assert (
        state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:a1").filename
        == filename
    )  # type: ignore[union-attr]


def test_unnamed_inline_attachment_body_is_accepted_when_marked_attachment(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    part = GmailPart(
        "1",
        "application/pdf",
        "",
        None,
        len(PDF),
        encoded(PDF),
        "attachment",
    )
    gateway.messages["m1"] = GmailMessage(
        "m1",
        "t1",
        "90",
        1000,
        "Files",
        "sender@example.com",
        GmailPart("", "multipart/mixed", "", None, None, children=(part,)),
        ("INBOX",),
    )
    service, state, importer = build_service(tmp_path, gateway)

    service.sync()

    assert importer.calls[0][0].filename.endswith(".pdf")
    assert (
        state.get_attachment(PROFILE, "personal", "m1", "1", "m1:1:inline") is not None
    )


def test_ocr_processing_reason_is_persisted_and_counted(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    importer = FakeImporter("ocr_required", processing_status="image_ocr_required")
    service, state, _ = build_service(tmp_path, gateway, importer=importer)

    report = service.sync()

    assert report.ocr_required == 1
    assert report.needs_attention == 1
    item = state.attention_items(PROFILE, "personal")[0]
    assert item.outcome == "ocr_required"
    assert item.processing_status == "image_ocr_required"
    assert state.counts(PROFILE, "personal")["ocr_required"] == 1


def test_delivery_is_at_least_once_and_idempotent_importer_absorbs_retry(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.message_pages[None] = MessagePage(("m1",), None)
    gateway.messages["m1"] = attachment_message()
    gateway.attachments[("m1", "a1")] = EncodedBody(encoded(PDF), len(PDF))
    state = FailFirstAttachmentState(tmp_path / "state")
    importer = SequenceImporter()
    service, _, _ = build_service(tmp_path, gateway, state, importer)

    with pytest.raises(OSError, match="state write failed"):
        service.sync()
    assert state.get_cursor(PROFILE, "personal") is None

    report = service.sync()
    assert len(importer.calls) == 2
    assert report.duplicates == 1
    assert state.get_cursor(PROFILE, "personal") == "100"
