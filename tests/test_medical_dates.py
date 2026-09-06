from datetime import date
from uuid import uuid4

import pytest

from health_agent.medical_dates import infer_medical_dates, recover_document_dates
from health_agent.models import Document, DocumentPage, Profile


def test_extended_collection_label() -> None:
    found = infer_medical_dates([(1, "Дата взятия биоматериала: 02.01.2000")])
    assert found.collected_date == date(2000, 1, 2)
    assert found.issued_date is None


def test_study_date_is_not_issue_date() -> None:
    found = infer_medical_dates([(1, "Дата исследования\n02.01.2000")])
    assert found.collected_date is None
    assert found.issued_date is None


def test_all_labels_and_formats() -> None:
    cases = [
        ("дата забора", "2.1.2000", "collected_date"),
        ("дата взятия материала", "02/01/2000", "collected_date"),
        ("collection date", "2000-01-02", "collected_date"),
        ("collected date", "02.01.2000", "collected_date"),
        ("specimen date", "02.01.2000", "collected_date"),
        ("дата выдачи результата", "02.01.2000", "issued_date"),
        ("дата выдачи заключения", "02.01.2000", "issued_date"),
        ("дата заключения", "02.01.2000", "issued_date"),
        ("issue date", "02.01.2000", "issued_date"),
        ("issued date", "02.01.2000", "issued_date"),
        ("report date", "02.01.2000", "issued_date"),
    ]
    for label, token, attribute in cases:
        found = infer_medical_dates([(1, f"{label}: {token}")])
        assert getattr(found, attribute) == date(2000, 1, 2)


def test_whole_label_line_and_crlf_offsets() -> None:
    text = "heading\r\nДата выдачи\r\n02.01.2000 12:34:56\r\nfooter"
    found = infer_medical_dates([(7, text)])
    assert found.issued_date == date(2000, 1, 2)
    evidence = found.evidence[0]
    assert (evidence.page_number, evidence.start, evidence.end) == (
        7,
        text.index("02.01.2000"),
        text.index("02.01.2000") + len("02.01.2000"),
    )


@pytest.mark.parametrize(
    "text",
    [
        "Дата выдачи: 02.01.20000",
        "Дата выдачи: 102.01.2000",
        "Дата выдачи: 02.001.2000",
        "Дата выдачи: 02.01.2000x",
        "xДата выдачи: 02.01.2000",
        "Дата выдачи\n\n02.01.2000",
        "Дата выдачи\nДата рождения: 02.01.2000",
        "Дата выдачи текста\n02.01.2000",
        "Дата готовности: 02.01.2000",
        "Дата выполнения: 02.01.2000",
        "Дата поступления: 02.01.2000",
        "Birth date: 02.01.2000",
        "Дата выдачи: 29.02.2025",
    ],
)
def test_rejects_unsafe_or_invalid_matches(text: str) -> None:
    found = infer_medical_dates([(1, text)], today=date(2026, 9, 6))
    assert found.collected_date is None
    assert found.issued_date is None
    assert not found.evidence


def test_future_duplicate_conflict_chronology_and_page_boundary() -> None:
    future = infer_medical_dates(
        [(1, "Issue date: 2026-09-07")], today=date(2026, 9, 6)
    )
    assert future.issued_date is None
    duplicate = infer_medical_dates(
        [(1, "Issue date: 02.01.2000"), (2, "Report date: 02/01/2000")]
    )
    assert duplicate.issued_date == date(2000, 1, 2)
    conflict = infer_medical_dates(
        [(1, "Issue date: 02.01.2000"), (2, "Issue date: 03.01.2000")]
    )
    assert conflict.issued_date is None
    assert conflict.blocked_roles == frozenset({"issued"})
    reversed_dates = infer_medical_dates(
        [(1, "Collection date: 03.01.2000\nIssue date: 02.01.2000")]
    )
    assert reversed_dates.collected_date is None
    assert reversed_dates.issued_date is None
    assert reversed_dates.blocked_roles == frozenset({"collected", "issued"})
    split = infer_medical_dates([(1, "Issue date"), (2, "02.01.2000")])
    assert split.issued_date is None


def _document(profile_id, suffix: str, *, collected=None, issued=None, error=None):
    return Document(
        id=uuid4(),
        profile_id=profile_id,
        sha256=suffix * 64,
        vault_path=f"vault/{suffix}",
        media_type="application/pdf",
        document_type="laboratory_report",
        collected_date=collected,
        issued_date=issued,
        processing_status="needs_review",
        safe_error_code=error,
    )


def test_recovery_is_scoped_dry_run_null_only_and_idempotent(session) -> None:
    local_id, foreign_id = uuid4(), uuid4()
    session.add_all(
        [Profile(id=local_id, name="Local"), Profile(id=foreign_id, name="Other")]
    )
    good = _document(local_id, "a")
    existing = _document(local_id, "b", collected=date(1999, 12, 31))
    conflict = _document(local_id, "c", error="conflicting_medical_date")
    foreign = _document(foreign_id, "d")
    session.add_all([good, existing, conflict, foreign])
    session.flush()
    for document in (good, existing, conflict, foreign):
        session.add(
            DocumentPage(
                document_id=document.id,
                page_number=1,
                extracted_text="Collection date: 01.01.2000\nIssue date: 02.01.2000",
                extraction_method="text",
            )
        )
    session.flush()

    dry = recover_document_dates(session, profile_id=local_id)
    assert dry == {"scanned": 3, "eligible": 2, "changed": 0, "blocked": 1}
    assert good.collected_date is None and good.issued_date is None

    applied = recover_document_dates(session, profile_id=local_id, apply=True)
    assert applied == {"scanned": 3, "eligible": 2, "changed": 2, "blocked": 1}
    assert (good.collected_date, good.issued_date) == (
        date(2000, 1, 1),
        date(2000, 1, 2),
    )
    assert existing.collected_date == date(1999, 12, 31)
    assert existing.issued_date == date(2000, 1, 2)
    assert conflict.safe_error_code == "conflicting_medical_date"
    assert foreign.collected_date is None and foreign.issued_date is None
    assert recover_document_dates(session, profile_id=local_id, apply=True) == {
        "scanned": 1,
        "eligible": 0,
        "changed": 0,
        "blocked": 1,
    }


@pytest.mark.parametrize("limit", [0, 501])
def test_recovery_rejects_out_of_range_limit(session, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        recover_document_dates(session, profile_id=uuid4(), limit=limit)
