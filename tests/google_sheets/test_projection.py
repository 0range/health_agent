from __future__ import annotations

from sqlalchemy.orm import Session

from health_agent.google_sheets.config import WORKBOOK_SCHEMA_VERSION
from health_agent.google_sheets.projection import SourceStatusRow, build_projection
from health_agent.google_sheets.types import WorkbookBinding
from health_agent.models import DEFAULT_PROFILE_ID, ReviewStatus

from .helpers import add_observation, add_profile


def _binding(profile_id=DEFAULT_PROFILE_ID) -> WorkbookBinding:
    return WorkbookBinding(str(profile_id), WORKBOOK_SCHEMA_VERSION, "binding_12345678")


def test_projection_is_profile_scoped_and_excludes_raw_medical_text(
    session: Session,
) -> None:
    own, _ = add_observation(
        session, DEFAULT_PROFILE_ID, status=ReviewStatus.VERIFIED, value="21"
    )
    other_profile = add_profile(session)
    other, _ = add_observation(
        session, other_profile, status=ReviewStatus.VERIFIED, value="999"
    )
    bundle = build_projection(session, DEFAULT_PROFILE_ID, _binding())
    rendered = repr(bundle)
    assert str(own.id) in rendered
    assert str(other.id) not in rendered
    assert all(
        value != "999"
        for sheet in bundle.workbook.sheets
        for row in sheet.rows
        for value in row
    )
    assert "PRIVATE BODY" not in rendered
    assert "PRIVATE EVIDENCE" not in rendered
    assert "/private/vault" not in rendered


def test_projection_separates_verified_history_and_pending_review(
    session: Session,
) -> None:
    _verified, _ = add_observation(
        session, DEFAULT_PROFILE_ID, status=ReviewStatus.VERIFIED
    )
    pending, review = add_observation(session, DEFAULT_PROFILE_ID)
    source = SourceStatusRow("whoop", "main", "ready", freshness="fresh")
    bundle = build_projection(session, DEFAULT_PROFILE_ID, _binding(), (source,))
    history, needs_review, sources = bundle.workbook.sheets
    assert history.rows[0][3] == "12.5"
    assert str(pending.id) not in repr(history.rows)
    assert needs_review.rows[0][0] == str(review.id)
    assert needs_review.rows[0][2] == str(DEFAULT_PROFILE_ID)
    assert needs_review.rows[0][12:] == ("", "", "", "")
    assert sources.rows == (source.values(),)


def test_projection_marks_missing_medical_date_without_using_import_time(
    session: Session,
) -> None:
    add_observation(
        session, DEFAULT_PROFILE_ID, status=ReviewStatus.VERIFIED, date_value=None
    )
    history = build_projection(session, DEFAULT_PROFILE_ID, _binding()).workbook.sheets[
        0
    ]
    assert history.rows[0][0] is None
