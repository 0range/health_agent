from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from health_agent.models import Profile
from health_agent.visits.models import HealthVisit
from health_agent.visits.repository import VisitNotFound, VisitRepository

PROFILE = UUID("00000000-0000-0000-0000-000000000001")
OTHER = UUID("00000000-0000-0000-0000-000000000002")
START = datetime(2026, 10, 5, 7, tzinfo=UTC)


def create(repo, profile=PROFILE, **changes):
    values = {
        "title": "Учебный визит",
        "starts_at": START,
        "ends_at": START + timedelta(hours=1),
        "timezone_name": "Europe/Moscow",
        "creation_key": "fixture-1",
    }
    values.update(changes)
    return repo.create(profile, **values)


def test_create_and_note_replay(session):
    repo = VisitRepository(session)
    visit = create(repo)
    assert create(repo) == visit
    assert session.scalar(select(func.count()).select_from(HealthVisit)) == 1
    with pytest.raises(ValueError):
        create(repo, title="Changed")
    note = repo.add_note(
        PROFILE,
        visit.public_code,
        kind="answer",
        text="Учебная запись",
        action_key="note-1",
    )
    assert (
        repo.add_note(
            PROFILE,
            visit.public_code,
            kind="answer",
            text="Учебная запись",
            action_key="note-1",
        )
        == note
    )
    assert repo.notes(PROFILE, visit.public_code) == (note,)
    with pytest.raises(ValueError):
        repo.add_note(
            PROFILE,
            visit.public_code,
            kind="question",
            text="Changed",
            action_key="note-1",
        )


def test_all_operations_are_profile_scoped(session):
    session.add(Profile(id=OTHER, name="Other"))
    session.flush()
    repo = VisitRepository(session)
    visit = create(repo)
    assert repo.list(OTHER) == ()
    with pytest.raises(ValueError):
        create(repo, OTHER)
    actions = [
        lambda: repo.get(OTHER, visit.public_code),
        lambda: repo.notes(OTHER, visit.public_code),
        lambda: repo.complete(OTHER, visit.public_code),
        lambda: repo.cancel(OTHER, visit.public_code),
        lambda: repo.reschedule(
            OTHER,
            visit.public_code,
            starts_at=START,
            ends_at=START + timedelta(hours=2),
            timezone_name="UTC",
        ),
        lambda: repo.add_note(
            OTHER, visit.public_code, kind="answer", text="x", action_key="foreign"
        ),
    ]
    for action in actions:
        with pytest.raises(VisitNotFound):
            action()


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "a" * 201},
        {"creation_key": "a" * 201},
        {"starts_at": START.replace(tzinfo=None)},
        {"ends_at": START},
        {"timezone_name": "Bad/Timezone"},
        {"timezone_name": "a" * 101},
    ],
)
def test_invalid_create_does_not_persist(session, changes):
    with pytest.raises(ValueError):
        create(VisitRepository(session), **changes)
    assert session.scalar(select(func.count()).select_from(HealthVisit)) == 0


def test_terminal_states_and_bounds(session):
    repo = VisitRepository(session)
    visit = create(repo)
    moved = repo.reschedule(
        PROFILE,
        visit.public_code,
        starts_at=START + timedelta(days=1),
        ends_at=START + timedelta(days=1, hours=2),
        timezone_name="UTC",
    )
    assert moved.starts_at == START + timedelta(days=1)
    done = repo.complete(PROFILE, visit.public_code)
    assert repo.complete(PROFILE, visit.public_code) == done
    repo.add_note(
        PROFILE,
        visit.public_code,
        kind="answer",
        text="After visit",
        action_key="after",
    )
    for action in [
        lambda: repo.cancel(PROFILE, visit.public_code),
        lambda: repo.reschedule(
            PROFILE,
            visit.public_code,
            starts_at=START,
            ends_at=START + timedelta(hours=1),
            timezone_name="UTC",
        ),
    ]:
        with pytest.raises(ValueError):
            action()
    second = create(repo, creation_key="second")
    repo.cancel(PROFILE, second.public_code)
    with pytest.raises(ValueError):
        repo.add_note(
            PROFILE, second.public_code, kind="question", text="Late", action_key="late"
        )
    for limit in (0, 101):
        with pytest.raises(ValueError):
            repo.list(PROFILE, limit=limit)
        with pytest.raises(ValueError):
            repo.notes(PROFILE, visit.public_code, limit=limit)
    with pytest.raises(ValueError):
        repo.add_note(
            PROFILE,
            visit.public_code,
            kind="answer",
            text="a" * 10001,
            action_key="big",
        )


def test_upcoming_first_and_note_validation(session):
    repo = VisitRepository(session)
    old = create(
        repo,
        starts_at=START.replace(year=2020),
        ends_at=START.replace(year=2020) + timedelta(hours=1),
    )
    future = datetime.now(UTC) + timedelta(days=30)
    upcoming = create(
        repo,
        creation_key="upcoming",
        starts_at=future,
        ends_at=future + timedelta(hours=1),
    )
    assert repo.list(PROFILE, limit=1) == (upcoming,)
    for kind, value, key in [
        ("diagnosis", "x", "valid"),
        ("answer", "", "valid"),
        ("answer", "x", "x" * 201),
    ]:
        with pytest.raises(ValueError):
            repo.add_note(
                PROFILE, old.public_code, kind=kind, text=value, action_key=key
            )
    assert repo.notes(PROFILE, old.public_code) == ()
