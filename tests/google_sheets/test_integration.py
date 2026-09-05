from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from health_agent.db import session_scope
from health_agent.google_sheets.models import SheetsReviewDecisionAudit
from health_agent.models import DEFAULT_PROFILE_ID, LabObservation, ReviewStatus

from .helpers import add_observation, add_profile
from .test_service import FakeGateway, _service


def test_profile_sheet_roundtrip_isolated_and_idempotent(
    tmp_path: Path, clean_database
) -> None:
    with session_scope(clean_database) as session:
        own = [
            add_observation(session, DEFAULT_PROFILE_ID, value=str(value))[0]
            for value in (11, 22, 33)
        ]
        other_profile = add_profile(session)
        other, _ = add_observation(session, other_profile, value="999")
        own_ids = [row.id for row in own]
        other_id = other.id

    gateway = FakeGateway()
    service = _service(tmp_path, clean_database, gateway)
    service.configure(
        DEFAULT_PROFILE_ID,
        expected_permission_id="permission-1",
        expected_email="me@example.com",
    )
    first = service.sync(DEFAULT_PROFILE_ID)
    assert first.review_rows == 3
    assert gateway.latest is not None
    assert all(
        value != "999"
        for sheet in gateway.latest.sheets
        for row in sheet.rows
        for value in row
    )

    header, *remote_rows = gateway.review_rows
    actions = (
        ("approve", "", "", ""),
        ("correct", "25", "ng/ml", "ferritin"),
        ("reject", "", "", ""),
    )
    gateway.review_rows = (
        header,
        *(
            tuple(row[:12]) + action
            for row, action in zip(remote_rows, actions, strict=True)
        ),
    )
    second = service.sync(DEFAULT_PROFILE_ID)
    assert second.decisions_applied == 3
    assert second.review_rows == 0
    third = service.sync(DEFAULT_PROFILE_ID)
    assert third.decisions_applied == third.decisions_replayed == 0
    assert gateway.created == 1

    with session_scope(clean_database) as session:
        statuses = {
            observation_id: status
            for observation_id, status in session.execute(
                select(LabObservation.id, LabObservation.status).where(
                    LabObservation.id.in_((*own_ids, other_id))
                )
            )
        }
        audit_profiles = session.scalars(
            select(SheetsReviewDecisionAudit.profile_id)
        ).all()
    assert statuses[own_ids[0]] == ReviewStatus.VERIFIED
    assert statuses[own_ids[1:][0]] == ReviewStatus.REJECTED
    assert statuses[own_ids[2]] == ReviewStatus.REJECTED
    assert statuses[other_id] == ReviewStatus.NEEDS_REVIEW
    assert len(audit_profiles) == 3
    assert all(profile_id == DEFAULT_PROFILE_ID for profile_id in audit_profiles)
