from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_food import Brain, MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach


def _photo(path: Path, name: str) -> Attachment:
    return Attachment(path / name, "image/jpeg")


def test_photo_comments_stay_on_one_meal_and_replay_is_bound(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    noon = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="photo1", now=noon, attachment=_photo(tmp_path, "one.jpg"))
    first = store.list(profile, "food", "meal")[0]
    coach.handle(profile, "Там ещё было немного масла", source_key="comment1", now=noon + timedelta(minutes=2))
    coach.handle(profile, "Порция примерно 250 г", source_key="comment2", now=noon + timedelta(minutes=3))

    comments = list(reversed(store.list(profile, "food", "comment")))
    meal = store.get(profile, first.id)
    assert meal is not None
    assert len(store.list(profile, "food", "meal")) == 1
    assert [item.payload["text"] for item in comments] == [
        "Там ещё было немного масла", "Порция примерно 250 г",
    ]
    assert all(item.payload["meal_id"] == first.id for item in comments)
    assert meal.payload["occurred_at"] == first.payload["occurred_at"]
    assert meal.payload["ended_at"] == (noon + timedelta(minutes=20)).isoformat()
    assert brain.calls[-1][0]["meal"]["comments"] == [item.payload["text"] for item in comments]

    later = noon + timedelta(hours=3)
    coach.handle(profile, "", source_key="photo2", now=later, attachment=_photo(tmp_path, "two.jpg"))
    calls = len(brain.calls)
    coach.handle(profile, "Там ещё было немного масла", source_key="comment1", now=later)
    assert len(brain.calls) == calls
    assert store.get(profile, first.id) == meal


@pytest.mark.parametrize(
    ("minutes", "meal_count", "needs_confirmation"),
    [(39, 1, False), (40, 1, True), (150, 1, True), (151, 2, False)],
)
def test_photo_gap_boundaries(
    tmp_path: Path, minutes: int, meal_count: int, needs_confirmation: bool,
) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    started = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="p1", now=started, attachment=_photo(tmp_path, "1.jpg"))
    reply = coach.handle(
        profile, "", source_key="p2", now=started + timedelta(minutes=minutes),
        attachment=_photo(tmp_path, "2.jpg"),
    )
    assert len(store.list(profile, "food", "meal")) == meal_count
    assert ("Это продолжение" in reply) is needs_confirmation
    assert len(store.list(profile, "food", "photo")) == 2
    if needs_confirmation:
        calls = len(brain.calls)
        answer = coach.handle(
            profile, "тот же", source_key="confirm", now=started + timedelta(minutes=minutes, seconds=10),
        )
        assert len(store.list(profile, "food", "meal")) == 1
        assert len(brain.calls) == calls + 1
        meal = store.list(profile, "food", "meal")[0]
        assert meal.payload["ended_at"] == (started + timedelta(minutes=minutes + 20)).isoformat()
        calls = len(brain.calls)
        assert coach.handle(
            profile, "тот же", source_key="confirm", now=started + timedelta(minutes=minutes, seconds=10),
        ) == answer
        assert len(brain.calls) == calls


def test_questions_and_uncertain_text_do_not_create_meals() -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    assert "Health Agent" in coach.handle(
        profile, "Почему я плохо сплю?", source_key="q", now=now,
    )
    assert "новый приём" in coach.handle(
        profile, "немного масла", source_key="unclear", now=now,
    ).lower()
    assert not store.list(profile, "food", "meal")
    assert store.list(profile, "food", "pending_text")[0].payload["text"] == "немного масла"
    assert not brain.calls

    reply = coach.handle(profile, "новый", source_key="confirm-text", now=now + timedelta(seconds=1))
    assert len(store.list(profile, "food", "meal")) == 1
    calls = len(brain.calls)
    assert coach.handle(
        profile, "новый", source_key="confirm-text", now=now + timedelta(seconds=1),
    ) == reply
    assert len(brain.calls) == calls


def test_ambiguous_photo_can_be_confirmed_as_new_without_touching_candidate(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    start = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="first", now=start, attachment=_photo(tmp_path, "1.jpg"))
    first = store.list(profile, "food", "meal")[0]
    first_before = dict(first.payload)
    coach.handle(
        profile, "", source_key="ambiguous", now=start + timedelta(minutes=40),
        attachment=_photo(tmp_path, "2.jpg"),
    )
    coach.handle(
        profile, "новый", source_key="new-confirm", now=start + timedelta(minutes=41),
    )
    assert len(store.list(profile, "food", "meal")) == 2
    assert store.get(profile, first.id).payload == first_before  # type: ignore[union-attr]
    newest = store.list(profile, "food", "meal")[0]
    assert newest.payload["photo_path"].endswith("2.jpg")


def test_failed_comment_reanalysis_retries_original_meal(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    start = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="p1", now=start, attachment=_photo(tmp_path, "1.jpg"))
    first = store.list(profile, "food", "meal")[0]
    brain.reply = RuntimeError("offline")
    coach.handle(profile, "масло", source_key="comment", now=start + timedelta(minutes=2))
    assert store.list(profile, "food", "comment")[0].payload["meal_id"] == first.id
    brain.reply = Brain().reply
    coach.handle(profile, "поел суп", source_key="new", now=start + timedelta(hours=3))
    newer = store.list(profile, "food", "meal")[0]
    newer_before = dict(newer.payload)
    coach.handle(profile, "масло", source_key="comment", now=start + timedelta(hours=3))
    assert store.get(profile, newer.id).payload == newer_before  # type: ignore[union-attr]
    assert store.get(profile, first.id).payload["analysis_status"] == "complete"  # type: ignore[union-attr]


def test_sunday_weekly_notice_is_cached_replayed_and_not_hidden_by_dinner() -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    sunday = datetime(2026, 9, 6, 15, tzinfo=UTC)  # 18:00 Moscow
    coach.handle(profile, "/ел 18:00 ужин", source_key="dinner", now=sunday)
    notices = coach.due(profile, sunday)
    assert len(notices) == 1
    assert notices[0].key == "food-weekly-2026-W36"
    assert "Сохранено" in notices[0].text
    calls = len(brain.calls)
    assert FoodCoach(store, brain).due(profile, sunday) == notices
    assert len(brain.calls) == calls
    store.put(profile, "food", "notice", notices[0].key, {"delivered": True}, at=sunday)
    assert coach.due(profile, sunday) == []


def test_weekly_silence_pause_outage_and_profile_isolation() -> None:
    store, profile, other = MemoryStore(), uuid4(), uuid4()
    brain = Brain(RuntimeError("offline"))
    coach = FoodCoach(store, brain)
    sunday = datetime(2026, 9, 6, 15, tzinfo=UTC)
    assert coach.due(profile, sunday) == []
    coach.handle(profile, "/ел 12:00 обед", source_key="meal", now=sunday.replace(hour=9))
    first = coach.due(profile, sunday)
    assert len(first) == 1 and "Сохранено" in first[0].text
    calls = len(brain.calls)
    assert coach.due(profile, sunday) == first
    assert len(brain.calls) == calls
    assert coach.due(other, sunday) == []
    coach.handle(profile, "/напоминания выкл", source_key="off", now=sunday)
    assert coach.due(profile, sunday) == []


def test_multiple_photos_are_preserved_and_latest_photo_anchors_reminder(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    start = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="p1", now=start, attachment=_photo(tmp_path, "1.jpg"))
    coach.handle(
        profile, "", source_key="p2", now=start + timedelta(minutes=39),
        attachment=_photo(tmp_path, "2.jpg"),
    )
    meal = store.list(profile, "food", "meal")[0]
    assert meal.payload["photo_path"].endswith("1.jpg")
    assert [item["path"] for item in meal.payload["photos"]] == [
        str(tmp_path / "1.jpg"), str(tmp_path / "2.jpg"),
    ]
    assert meal.payload["ended_at"] == (start + timedelta(minutes=59)).isoformat()
    assert coach.due(profile, start + timedelta(minutes=20, hours=3, seconds=1)) == []
    assert coach.due(profile, start + timedelta(hours=4, minutes=30))
    assert all(call[1] in {tmp_path / "1.jpg", tmp_path / "2.jpg"} for call in brain.calls)
