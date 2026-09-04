from __future__ import annotations

import base64

import pytest

from health_agent.gmail.classifier import classify_attachment, classify_message
from health_agent.gmail.types import GmailMessage, GmailPart


def message(subject: str, sender: str = "sender@example.com") -> GmailMessage:
    payload = GmailPart("", "multipart/mixed", "", None, None)
    return GmailMessage("m1", "t1", "10", 1000, subject, sender, payload)


@pytest.mark.parametrize(
    ("subject", "filename"),
    (
        ("Your documents", "Результаты анализов.pdf"),
        ("Laboratory report", "document.pdf"),
        ("МРТ — заключение", "scan.jpg"),
    ),
)
def test_strong_medical_metadata_routes_supported_file(
    subject: str, filename: str
) -> None:
    part = GmailPart("1", "application/pdf", filename, "a1", 5)
    result = classify_attachment(message(subject), part, ())
    assert result.decision == "suspected_medical"


def test_trusted_sender_routes_generic_supported_file() -> None:
    part = GmailPart("1", "application/octet-stream", "result.pdf", "a1", 5)
    result = classify_attachment(
        message("Your files", "lab@example.com"), part, ("lab@example.com",)
    )
    assert result.decision == "suspected_medical"
    assert result.effective_mime_type == "application/pdf"


def test_generic_supported_attachment_stays_internal_ambiguity() -> None:
    part = GmailPart("1", "application/pdf", "document.pdf", "a1", 5)
    result = classify_attachment(message("Your files"), part, ())
    assert result.decision == "ambiguous"


def test_inline_image_and_unsupported_file_are_ignored() -> None:
    logo = GmailPart("1", "image/png", "logo.png", None, 5, disposition="inline")
    archive = GmailPart("2", "application/zip", "labs.zip", "a2", 5)
    assert (
        classify_attachment(message("Lab results"), logo, ()).reason_code
        == "inline_image"
    )
    assert (
        classify_attachment(message("Lab results"), archive, ()).reason_code
        == "unsupported_mime"
    )


def test_body_only_appointment_is_classified_without_persisting_body() -> None:
    body = "Напоминаем: приём у терапевта завтра".encode()
    payload = GmailPart(
        "",
        "text/plain",
        "",
        None,
        len(body),
        base64.urlsafe_b64encode(body).decode().rstrip("="),
    )
    item = GmailMessage(
        "m1", "t1", "10", 1000, "Reminder", "clinic@example.com", payload
    )
    assert classify_message(item).decision == "appointment"
