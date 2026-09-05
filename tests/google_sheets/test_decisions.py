from __future__ import annotations

import threading
from datetime import date

import pytest
from sqlalchemy import event, select
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
from health_agent.importer import reject_observation
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


@pytest.mark.parametrize("column", (12, 13))
def test_parser_rejects_formula_like_editable_cells(
    session: Session, column: int
) -> None:
    add_observation(session, DEFAULT_PROFILE_ID)
    bundle = _bundle(session)
    decision_formula = list(bundle.pending_reviews[0].values())
    decision_formula[column] = '=IF(TRUE,"approve","")'
    with pytest.raises(ReviewGridError, match="formulas"):
        parse_decisions(
            (REVIEW_HEADERS, tuple(decision_formula)),
            bundle.known_reviews,
            DEFAULT_PROFILE_ID,
        )


def test_parser_accepts_trusted_literal_equals_in_immutable_unit(
    session: Session,
) -> None:
    add_observation(session, DEFAULT_PROFILE_ID, source_unit="=synthetic-unit")
    bundle = _bundle(session)
    assert (
        parse_decisions(
            (REVIEW_HEADERS, bundle.pending_reviews[0].values()),
            bundle.known_reviews,
            DEFAULT_PROFILE_ID,
        )
        == ()
    )


def test_parser_rejects_changed_literal_equals_in_immutable_unit(
    session: Session,
) -> None:
    add_observation(session, DEFAULT_PROFILE_ID, source_unit="=synthetic-unit")
    bundle = _bundle(session)
    row = list(bundle.pending_reviews[0].values())
    row[6] = "=changed-unit"
    with pytest.raises(ReviewGridError, match="ownership or version"):
        parse_decisions(
            (REVIEW_HEADERS, tuple(row)), bundle.known_reviews, DEFAULT_PROFILE_ID
        )


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


def test_concurrent_core_rejection_cannot_be_overwritten_by_sheet_decision(
    clean_database,
) -> None:
    with Session(clean_database) as setup, setup.begin():
        observation, _review = add_observation(setup, DEFAULT_PROFILE_ID)
        observation_id = observation.id
    with Session(clean_database) as reader:
        bundle = _bundle(reader)
        decisions = parse_decisions(
            _grid(bundle, (("approve", None, None, None),)),
            bundle.known_reviews,
            DEFAULT_PROFILE_ID,
        )

    owner = Session(clean_database)
    owner.begin()
    owner.scalar(
        select(LabObservation)
        .where(LabObservation.id == observation_id)
        .with_for_update()
    )
    worker_query_started = threading.Event()
    errors: list[BaseException] = []

    def before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):  # type: ignore[no-untyped-def]
        del conn, cursor, parameters, context, executemany
        if (
            threading.current_thread().name == "sheets-review"
            and "FOR UPDATE" in statement
        ):
            worker_query_started.set()

    def apply_sheet_decision() -> None:
        try:
            with Session(clean_database) as worker:
                apply_decisions(
                    worker,
                    DEFAULT_PROFILE_ID,
                    "spreadsheet_123",
                    decisions,
                )
                worker.commit()
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    event.listen(clean_database, "before_cursor_execute", before_cursor_execute)
    worker = threading.Thread(target=apply_sheet_decision, name="sheets-review")
    try:
        worker.start()
        assert worker_query_started.wait(timeout=5)
        reject_observation(owner, observation_id, profile_id=DEFAULT_PROFILE_ID)
        owner.commit()
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        event.remove(clean_database, "before_cursor_execute", before_cursor_execute)
        owner.close()
    assert len(errors) == 1
    assert isinstance(errors[0], ReviewConflict)
    with Session(clean_database) as verify:
        assert (
            verify.get_one(LabObservation, observation_id).status
            == ReviewStatus.REJECTED
        )
        assert verify.scalars(select(SheetsReviewDecisionAudit)).all() == []


def test_retained_identity_map_state_cannot_overwrite_committed_rejection(
    clean_database,
) -> None:
    with Session(clean_database) as setup, setup.begin():
        observation, _review = add_observation(setup, DEFAULT_PROFILE_ID)
        observation_id = observation.id

    stale = Session(clean_database)
    try:
        retained = stale.get_one(LabObservation, observation_id)
        bundle = _bundle(stale)
        decisions = parse_decisions(
            _grid(bundle, (("approve", None, None, None),)),
            bundle.known_reviews,
            DEFAULT_PROFILE_ID,
        )
        assert retained.status == ReviewStatus.NEEDS_REVIEW

        with Session(clean_database) as concurrent, concurrent.begin():
            reject_observation(
                concurrent, observation_id, profile_id=DEFAULT_PROFILE_ID
            )

        with pytest.raises(ReviewConflict):
            apply_decisions(
                stale,
                DEFAULT_PROFILE_ID,
                "spreadsheet_123",
                decisions,
            )
        stale.rollback()
    finally:
        stale.close()

    with Session(clean_database) as verify:
        assert (
            verify.get_one(LabObservation, observation_id).status
            == ReviewStatus.REJECTED
        )
        assert verify.scalars(select(SheetsReviewDecisionAudit)).all() == []


def test_stale_medical_date_rolls_back_entire_decision_batch(session: Session) -> None:
    first, _ = add_observation(session, DEFAULT_PROFILE_ID, value="11")
    second, _ = add_observation(session, DEFAULT_PROFILE_ID, value="22")
    bundle = _bundle(session)
    decisions = parse_decisions(
        _grid(
            bundle,
            (
                ("approve", None, None, None),
                ("reject", None, None, None),
            ),
        ),
        bundle.known_reviews,
        DEFAULT_PROFILE_ID,
    )
    second.document.collected_date = date(2026, 9, 5)
    session.flush()

    with pytest.raises(ReviewConflict):
        apply_decisions(session, DEFAULT_PROFILE_ID, "spreadsheet_123", decisions)
    session.expire_all()
    assert session.get_one(LabObservation, first.id).status == ReviewStatus.NEEDS_REVIEW
    assert (
        session.get_one(LabObservation, second.id).status == ReviewStatus.NEEDS_REVIEW
    )
    assert session.scalars(select(SheetsReviewDecisionAudit)).all() == []
