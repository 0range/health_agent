from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.google_sheets.config import WORKBOOK_SCHEMA_VERSION
from health_agent.google_sheets.decisions import (
    ReviewConflict,
    ReviewGridError,
    apply_decisions,
    parse_decisions,
)
from health_agent.google_sheets.models import SheetsReviewDecisionAudit
from health_agent.google_sheets.projection import REVIEW_HEADERS, build_projection
from health_agent.google_sheets.types import WorkbookBinding
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus

from .helpers import add_observation


def _bundle(session: Session):
    return build_projection(
        session,
        DEFAULT_PROFILE_ID,
        WorkbookBinding(
            str(DEFAULT_PROFILE_ID), WORKBOOK_SCHEMA_VERSION, "binding_12345678"
        ),
    )


def _grid(
    bundle, decisions: tuple[tuple[str, str | None, str | None, str | None], ...]
):
    rows = []
    for expected, decision in zip(bundle.pending_reviews, decisions, strict=True):
        rows.append(expected.immutable_values + decision)
    return (REVIEW_HEADERS, *rows)


@pytest.mark.parametrize(
    "column,value",
    [(0, "unknown"), (2, "00000000-0000-0000-0000-000000000099"), (3, "stale")],
)
def test_parser_rejects_tampered_machine_fields(
    session: Session, column: int, value: str
) -> None:
    add_observation(session, DEFAULT_PROFILE_ID)
    bundle = _bundle(session)
    row = list(bundle.pending_reviews[0].values())
    row[column] = value
    with pytest.raises(ReviewGridError):
        parse_decisions(
            (REVIEW_HEADERS, tuple(row)), bundle.known_reviews, DEFAULT_PROFILE_ID
        )


def test_parser_rejects_duplicate_and_malformed_corrections(session: Session) -> None:
    add_observation(session, DEFAULT_PROFILE_ID)
    bundle = _bundle(session)
    row = bundle.pending_reviews[0].immutable_values + ("approve", "99", "", "")
    with pytest.raises(ReviewGridError):
        parse_decisions((REVIEW_HEADERS, row), bundle.known_reviews, DEFAULT_PROFILE_ID)
    clean = bundle.pending_reviews[0].values()
    with pytest.raises(ReviewGridError, match="duplicate"):
        parse_decisions(
            (REVIEW_HEADERS, clean, clean), bundle.known_reviews, DEFAULT_PROFILE_ID
        )


def test_parser_rejects_missing_header(session: Session) -> None:
    add_observation(session, DEFAULT_PROFILE_ID)
    with pytest.raises(ReviewGridError, match="schema"):
        parse_decisions((), _bundle(session).known_reviews, DEFAULT_PROFILE_ID)


def test_apply_mixed_batch_and_identical_replay(session: Session) -> None:
    observations = [
        add_observation(session, DEFAULT_PROFILE_ID, value=str(value))[0]
        for value in (11, 22, 33)
    ]
    bundle = _bundle(session)
    grid = _grid(
        bundle,
        (
            ("approve", None, None, None),
            ("correct", "25", "ng/ml", "ferritin"),
            ("reject", None, None, None),
        ),
    )
    decisions = parse_decisions(grid, bundle.known_reviews, DEFAULT_PROFILE_ID)
    report = apply_decisions(session, DEFAULT_PROFILE_ID, "spreadsheet_123", decisions)
    assert (report.approved, report.corrected, report.rejected, report.replayed) == (
        1,
        1,
        1,
        0,
    )
    assert (
        apply_decisions(
            session, DEFAULT_PROFILE_ID, "spreadsheet_123", decisions
        ).replayed
        == 3
    )
    session.expire_all()
    statuses = [
        session.get_one(LabObservation, observation.id).status
        for observation in observations
    ]
    assert statuses == [
        ReviewStatus.VERIFIED,
        ReviewStatus.REJECTED,
        ReviewStatus.REJECTED,
    ]
    assert len(session.scalars(select(SheetsReviewDecisionAudit)).all()) == 3


def test_conflicting_replay_fails_closed(session: Session) -> None:
    add_observation(session, DEFAULT_PROFILE_ID)
    bundle = _bundle(session)
    first = parse_decisions(
        _grid(bundle, (("approve", None, None, None),)),
        bundle.known_reviews,
        DEFAULT_PROFILE_ID,
    )
    apply_decisions(session, DEFAULT_PROFILE_ID, "spreadsheet_123", first)
    second = parse_decisions(
        _grid(bundle, (("reject", None, None, None),)),
        bundle.known_reviews,
        DEFAULT_PROFILE_ID,
    )
    with pytest.raises(ReviewConflict):
        apply_decisions(session, DEFAULT_PROFILE_ID, "spreadsheet_123", second)
