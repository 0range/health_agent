from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_food import Brain, MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach


def _photo(path: Path, name: str) -> Attachment:
    return Attachment(path / name, "image/jpeg")


@pytest.mark.parametrize("kind", ["comment", "photo", "photo_confirmation", "text_confirmation"])
@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("original_time", [False, True])
def test_durable_replay_beyond_history_caps(tmp_path, kind, offline, original_time):
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="first", now=now, attachment=_photo(tmp_path, "first.jpg"))
    first = store.list(profile, "food", "meal")[0]
    if kind == "photo_confirmation":
        coach.handle(profile, "", source_key="ambiguous", now=now + timedelta(minutes=40), attachment=_photo(tmp_path, "second.jpg"))
    if kind == "text_confirmation":
        coach.handle(profile, "масло", source_key="pending", now=now + timedelta(days=1))
    brain.reply = RuntimeError("offline") if offline else Brain().reply
    event = now + (timedelta(days=1, minutes=1) if kind == "text_confirmation" else timedelta(minutes=41))
    text = {"comment": "немного масла", "photo": "", "photo_confirmation": "тот же", "text_confirmation": "новый"}[kind]
    attachment = _photo(tmp_path, "retry.jpg") if kind == "photo" else None
    if kind == "photo":
        event = now + timedelta(minutes=2)
    coach.handle(profile, text, source_key="replay", now=event, attachment=attachment)
    binding = next(r for _, r in store.records if r.kind == kind and r.source_key == "replay")
    original = store.get(profile, binding.payload["meal_id"])
    for index in range(1001):
        store.put(profile, "food", kind, f"filler{index}", {"meal_id": "unrelated", "text": "filler", "status": "confirmed"}, at=event + timedelta(seconds=index + 1))
    brain.reply = Brain().reply
    coach.handle(profile, "поел суп", source_key="newer", now=event + timedelta(days=2))
    newer = store.list(profile, "food", "meal")[0]
    before = json.dumps(newer.payload, sort_keys=True)
    calls = len(brain.calls)
    count = len(store.records)
    replay_at = event if original_time else event + timedelta(days=2)
    coach.handle(profile, text, source_key="replay", now=replay_at, attachment=attachment)
    assert len(store.records) == count
    assert json.dumps(store.get(profile, newer.id).payload, sort_keys=True) == before
    assert len(brain.calls) == calls + int(offline)
    assert store.get(profile, original.id).payload["analysis_status"] == "complete"
    if kind == "comment" and offline:
        assert "немного масла" in brain.calls[-1][0]["meal"]["comments"]
    assert store.get(profile, first.id).payload["occurred_at"] == first.payload["occurred_at"]


def test_comment_honors_binding_returned_by_idempotent_put(monkeypatch):
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "поел рис", source_key="first", now=now)
    coach.handle(profile, "масло", source_key="comment", now=now + timedelta(minutes=1))
    coach.handle(profile, "поел суп", source_key="second", now=now + timedelta(hours=3))
    newer = store.list(profile, "food", "meal")[0]
    before = json.dumps(newer.payload, sort_keys=True)
    calls = len(brain.calls)
    # Simulate a lookup/insert race: the insert returns the already bound comment.
    monkeypatch.setattr(store, "by_source", lambda *args: None)
    coach._comment(profile, newer, "изменённый текст", "comment", now + timedelta(hours=3))
    assert json.dumps(store.get(profile, newer.id).payload, sort_keys=True) == before
    assert len(brain.calls) == calls


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


class SequenceBrain(Brain):
    def __init__(self, replies: list[str | Exception]) -> None:
        super().__init__()
        self.replies = replies

    def __call__(self, system: str, payload: dict[str, Any], *, image_path: Path | None = None) -> str:
        self.calls.append((payload, image_path))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _analysis(foods: list[str], caption: str = "") -> str:
    return json.dumps({
        "foods": foods, "plate_components": [], "portion_estimate": caption or None,
        "kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None,
        "saturated_fat_g": None, "fiber_g": None, "cholesterol_mg": None,
        "confidence": 0.4, "unknowns": ["количество"], "feedback": "ignored",
    }, ensure_ascii=False)


def test_distinct_photo_observations_are_immutable_and_aggregated(tmp_path: Path) -> None:
    store, profile = MemoryStore(), uuid4()
    brain = SequenceBrain([_analysis(["рис", "овощи"]), _analysis(["торт"]), _analysis(["масло"])])
    coach = FoodCoach(store, brain)
    start = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="main", now=start, attachment=Attachment(tmp_path / "main.jpg", "image/jpeg", "основное"))
    coach.handle(profile, "", source_key="dessert", now=start + timedelta(minutes=10), attachment=Attachment(tmp_path / "cake.jpg", "image/jpeg", "десерт"))
    before = store.list(profile, "food", "meal")[0]
    observations = [item["analysis"] for item in before.payload["photos"]]
    assert observations[0]["foods"] == ["рис", "овощи"]
    assert observations[1]["foods"] == ["торт"]
    assert set(before.payload["analysis"]["foods"]) == {"рис", "овощи", "торт"}
    assert brain.calls[-1][0]["meal"]["caption"] == "десерт"
    coach.handle(profile, "ещё масло", source_key="oil", now=start + timedelta(minutes=11))
    after = store.list(profile, "food", "meal")[0]
    assert [item["analysis"] for item in after.payload["photos"]] == observations
    assert set(after.payload["analysis"]["foods"]) >= {"рис", "овощи", "торт", "масло"}
    assert len(after.payload["analysis_revisions"]) == 3


def test_late_photo_binds_by_event_time_and_never_moves_anchor_back(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    start = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="a", now=start, attachment=_photo(tmp_path, "a.jpg"))
    coach.handle(profile, "", source_key="b", now=start + timedelta(minutes=151), attachment=_photo(tmp_path, "b.jpg"))
    newer = store.list(profile, "food", "meal")[0]
    newer_before = dict(newer.payload)
    coach.handle(profile, "", source_key="late", now=start + timedelta(minutes=10), attachment=_photo(tmp_path, "late.jpg"))
    assert store.get(profile, newer.id).payload == newer_before  # type: ignore[union-attr]
    older = min(store.list(profile, "food", "meal"), key=lambda item: item.at)
    assert older.payload["latest_photo_at"] == (start + timedelta(minutes=10)).isoformat()

    coach.handle(profile, "", source_key="pending", now=start + timedelta(minutes=50), attachment=_photo(tmp_path, "pending.jpg"))
    coach.handle(profile, "", source_key="later-confirmed", now=start + timedelta(minutes=20), attachment=_photo(tmp_path, "later.jpg"))
    anchor = store.get(profile, older.id).payload["latest_photo_at"]  # type: ignore[union-attr]
    coach.handle(profile, "тот же", source_key="pending-confirm", now=start + timedelta(minutes=51))
    assert store.get(profile, older.id).payload["latest_photo_at"] >= anchor  # type: ignore[union-attr]


@pytest.mark.parametrize("text", ["там рис", "Это рис?", "хочу суп"])
def test_food_words_are_not_automatically_new_intake(text: str, tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    if text != "хочу суп":
        coach.handle(profile, "", source_key="plate", now=now, attachment=_photo(tmp_path, "plate.jpg"))
    before = len(store.list(profile, "food", "meal"))
    coach.handle(profile, text, source_key="text", now=now + timedelta(minutes=1))
    assert len(store.list(profile, "food", "meal")) == before


def test_durable_comment_and_confirmation_replays_retry_next_day(tmp_path: Path) -> None:
    store, profile = MemoryStore(), uuid4()
    brain = SequenceBrain([_analysis(["рис"]), RuntimeError("offline"), _analysis(["рис", "масло"])])
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="photo", now=now, attachment=_photo(tmp_path, "one.jpg"))
    coach.handle(profile, "масло", source_key="comment", now=now + timedelta(minutes=1))
    meal = store.list(profile, "food", "meal")[0]
    assert meal.payload["analysis_status"] == "incomplete"
    coach.handle(profile, "масло", source_key="comment", now=now + timedelta(days=1))
    assert store.get(profile, meal.id).payload["analysis_status"] == "complete"  # type: ignore[union-attr]

    brain.replies = [RuntimeError("offline"), _analysis(["торт"])]
    coach.handle(profile, "", source_key="amb", now=now + timedelta(minutes=40), attachment=_photo(tmp_path, "amb.jpg"))
    coach.handle(profile, "тот же", source_key="confirm", now=now + timedelta(minutes=41))
    calls = len(brain.calls)
    coach.handle(profile, "тот же", source_key="confirm", now=now + timedelta(days=1))
    assert len(brain.calls) == calls + 1


def test_weekly_quiet_hours_and_manual_shared_evidence() -> None:
    store, brain, profile = MemoryStore(), Brain(RuntimeError("offline")), uuid4()
    coach = FoodCoach(store, brain)
    sunday = datetime(2026, 9, 6, 15, tzinfo=UTC)
    coach.handle(profile, "/ел 12:00 обед: рис", source_key="meal", now=sunday.replace(hour=9))
    assert coach.due(profile, sunday.replace(hour=20)) == []  # 23:00 Moscow
    manual = coach.handle(profile, "/неделя", source_key="manual", now=sunday)
    automatic = coach.due(profile, sunday)[0].text
    for value in (manual, automatic):
        assert "2026-09-06" in value
        assert "разнообраз" in value.lower()
        assert "нутриент" in value.lower()


def test_explicit_text_meal_is_a_boundary_for_following_photo(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    start = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "", source_key="breakfast", now=start, attachment=_photo(tmp_path, "first.jpg"))
    coach.handle(profile, "поел суп", source_key="lunch", now=start + timedelta(minutes=10))
    lunch = store.list(profile, "food", "meal")[0]
    coach.handle(
        profile, "", source_key="lunch-photo", now=start + timedelta(minutes=20),
        attachment=_photo(tmp_path, "soup.jpg"),
    )
    assert len(store.list(profile, "food", "meal")) == 2
    assert store.get(profile, lunch.id).payload["photo_path"].endswith("soup.jpg")  # type: ignore[union-attr]
    breakfast = min(store.list(profile, "food", "meal"), key=lambda item: item.at)
    assert len(breakfast.payload["photos"]) == 1


def test_stale_text_only_meal_does_not_capture_a_new_photo(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    old = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "/ел суп", source_key="old-text", now=old)
    old_meal = store.list(profile, "food", "meal")[0]
    old_before = dict(old_meal.payload)

    fresh = old + timedelta(days=7)
    coach.handle(
        profile, "", source_key="fresh-photo", now=fresh,
        attachment=_photo(tmp_path, "fresh.jpg"),
    )
    assert len(store.list(profile, "food", "meal")) == 2
    assert store.get(profile, old_meal.id).payload == old_before  # type: ignore[union-attr]
    newest = store.list(profile, "food", "meal")[0]
    assert newest.payload["occurred_at"] == fresh.isoformat()
    assert newest.payload["photo_path"].endswith("fresh.jpg")


def test_question_containing_intake_verb_does_not_create_meal() -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    reply = FoodCoach(store, brain).handle(
        profile, "Почему я поел и хочу спать?", source_key="question",
        now=datetime(2026, 9, 7, 9, tzinfo=UTC),
    )
    assert "Health Agent" in reply
    assert not store.list(profile, "food", "meal")
    assert not brain.calls


def test_weekly_long_food_names_fit_limit_and_keep_caveat_and_next_step() -> None:
    store, profile = MemoryStore(), uuid4()
    brain = Brain(RuntimeError("offline"))
    coach = FoodCoach(store, brain)
    sunday = datetime(2026, 9, 6, 15, tzinfo=UTC)
    long_foods = [f"продукт-{index}-" + "я" * 180 for index in range(8)]
    analysis = json.loads(_analysis(long_foods))
    store.put(profile, "food", "meal", "long", {
        "occurred_at": sunday.isoformat(), "category": "dinner", "analysis": analysis,
    }, at=sunday)
    for text in (
        coach.handle(profile, "/неделя", source_key="manual-long", now=sunday),
        coach.due(profile, sunday)[0].text,
    ):
        assert len(text) <= 1200
        assert "Это только записи" in text
        assert "Следующий шаг" in text
