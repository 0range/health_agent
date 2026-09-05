"""Strict review-grid validation and audited transactional application."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.google_sheets.models import SheetsReviewDecisionAudit
from health_agent.google_sheets.projection import (
    REVIEW_HEADERS,
    ExpectedReviewRow,
    locked_expected_review,
)
from health_agent.google_sheets.types import SheetValue
from health_agent.importer import (
    approve_observation,
    correct_observation,
    reject_observation,
)
from health_agent.models import Document, LabObservation

DecisionAction = Literal["approve", "correct", "reject"]


class ReviewGridError(ValueError):
    """Remote grid cannot be trusted as a decision source."""


class ReviewConflict(ReviewGridError):
    """A row has already been resolved with a different decision."""


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    review_item_id: UUID
    observation_id: UUID
    row_version: str
    sheet_row: int
    action: DecisionAction
    corrected_value: str | None
    corrected_unit: str | None
    corrected_canonical_name: str | None
    decision_hash: str


@dataclass(frozen=True, slots=True)
class DecisionReport:
    approved: int = 0
    corrected: int = 0
    rejected: int = 0
    replayed: int = 0

    @property
    def applied(self) -> int:
        return self.approved + self.corrected + self.rejected


def _text(value: SheetValue) -> str:
    return "" if value is None else str(value).strip()


def _decision_hash(
    row_version: str,
    action: str,
    corrected_value: str | None,
    corrected_unit: str | None,
    corrected_name: str | None,
) -> str:
    payload = json.dumps(
        [row_version, action, corrected_value, corrected_unit, corrected_name],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_decisions(
    rows: tuple[tuple[SheetValue, ...], ...],
    expected_rows: tuple[ExpectedReviewRow, ...],
    profile_id: UUID,
) -> tuple[ReviewDecision, ...]:
    if not rows:
        raise ReviewGridError("review sheet schema is missing")
    if tuple(_text(value) for value in rows[0]) != REVIEW_HEADERS:
        raise ReviewGridError("review sheet schema mismatch")
    expected = {str(row.review_item_id): row for row in expected_rows}
    seen: set[str] = set()
    decisions: list[ReviewDecision] = []
    width = len(REVIEW_HEADERS)
    for sheet_row, raw in enumerate(rows[1:], start=2):
        padded = tuple(raw) + (None,) * max(0, width - len(raw))
        if len(padded) != width:
            raise ReviewGridError("review row width mismatch")
        if not any(_text(value) for value in padded):
            continue
        if any(
            isinstance(value, str) and value.lstrip().startswith("=")
            for value in padded[12:]
        ):
            raise ReviewGridError("formulas are not allowed in the review sheet")
        review_id = _text(padded[0])
        if review_id in seen:
            raise ReviewGridError("duplicate review item")
        seen.add(review_id)
        expected_row = expected.get(review_id)
        if expected_row is None:
            raise ReviewGridError("unknown review item")
        immutable = tuple(_text(value) for value in padded[:12])
        expected_immutable = tuple(
            _text(value) for value in expected_row.immutable_values
        )
        if immutable != expected_immutable or _text(padded[2]) != str(profile_id):
            raise ReviewGridError("review row ownership or version mismatch")
        action = _text(padded[12]).casefold()
        correction = tuple(_text(value) or None for value in padded[13:16])
        if not action:
            if any(correction):
                raise ReviewGridError("correction requires a decision")
            continue
        if action not in {"approve", "correct", "reject"}:
            raise ReviewGridError("invalid review decision")
        if action == "correct":
            if correction[0] is None:
                raise ReviewGridError("correction requires a value")
        elif any(correction):
            raise ReviewGridError("correction fields require correct decision")
        decisions.append(
            ReviewDecision(
                expected_row.review_item_id,
                expected_row.observation_id,
                expected_row.row_version,
                sheet_row,
                action,  # type: ignore[arg-type]
                correction[0],
                correction[1],
                correction[2],
                _decision_hash(expected_row.row_version, action, *correction),
            )
        )
    return tuple(decisions)


def apply_decisions(
    session: Session,
    profile_id: UUID,
    spreadsheet_id: str,
    decisions: tuple[ReviewDecision, ...],
) -> DecisionReport:
    approved = corrected = rejected = replayed = 0
    transaction = (
        session.begin_nested() if session.in_transaction() else session.begin()
    )
    with transaction:
        for decision in decisions:
            current = locked_expected_review(
                session,
                profile_id,
                decision.review_item_id,
                decision.observation_id,
                decision.sheet_row,
            )
            existing = session.scalar(
                select(SheetsReviewDecisionAudit).where(
                    SheetsReviewDecisionAudit.profile_id == profile_id,
                    SheetsReviewDecisionAudit.review_item_id == decision.review_item_id,
                )
            )
            if existing is not None:
                if existing.decision_hash != decision.decision_hash:
                    raise ReviewConflict("review decision conflicts with applied audit")
                replayed += 1
                continue
            if current is None or current.row_version != decision.row_version:
                raise ReviewConflict("review row changed before decision was applied")
            document_id = session.scalar(
                select(LabObservation.document_id)
                .join(Document, LabObservation.document_id == Document.id)
                .where(
                    LabObservation.id == decision.observation_id,
                    Document.profile_id == profile_id,
                )
            )
            if document_id is None:
                raise ReviewGridError("review observation ownership mismatch")
            if decision.action == "approve":
                approve_observation(
                    session, decision.observation_id, profile_id=profile_id
                )
                approved += 1
                correction_json = None
            elif decision.action == "reject":
                reject_observation(
                    session, decision.observation_id, profile_id=profile_id
                )
                rejected += 1
                correction_json = None
            else:
                assert decision.corrected_value is not None
                correct_observation(
                    session,
                    decision.observation_id,
                    profile_id=profile_id,
                    source_value=decision.corrected_value,
                    source_unit=decision.corrected_unit,
                    canonical_name=decision.corrected_canonical_name,
                )
                corrected += 1
                correction_json = {
                    "source_value": decision.corrected_value,
                    "source_unit": decision.corrected_unit,
                    "canonical_name": decision.corrected_canonical_name,
                }
            session.add(
                SheetsReviewDecisionAudit(
                    profile_id=profile_id,
                    review_item_id=decision.review_item_id,
                    observation_id=decision.observation_id,
                    document_id=document_id,
                    spreadsheet_id=spreadsheet_id,
                    sheet_row=decision.sheet_row,
                    row_version=decision.row_version,
                    action=decision.action,
                    decision_hash=decision.decision_hash,
                    correction_json=correction_json,
                )
            )
        session.flush()
    return DecisionReport(approved, corrected, rejected, replayed)
