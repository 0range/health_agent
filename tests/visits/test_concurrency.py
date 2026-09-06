from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.visits.repository import VisitRepository


def test_concurrent_creation_and_note_replays_commit_one_record(clean_database):
    barrier = Barrier(2)
    start = datetime(2026, 10, 5, tzinfo=UTC)

    def create():
        barrier.wait(timeout=5)
        with session_scope(clean_database) as session:
            return VisitRepository(session).create(
                DEFAULT_PROFILE_ID,
                title="Concurrent",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                timezone_name="UTC",
                creation_key="concurrent",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: create(), range(2)))
    assert first == second
    barrier = Barrier(2)

    def note():
        barrier.wait(timeout=5)
        with session_scope(clean_database) as session:
            return VisitRepository(session).add_note(
                DEFAULT_PROFILE_ID,
                first.public_code,
                kind="answer",
                text="Concurrent note",
                action_key="concurrent-note",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_note, second_note = list(pool.map(lambda _: note(), range(2)))
    assert first_note == second_note
    with session_scope(clean_database) as session:
        repo = VisitRepository(session)
        assert len(repo.list(DEFAULT_PROFILE_ID)) == 1
        assert repo.notes(DEFAULT_PROFILE_ID, first.public_code) == (first_note,)
