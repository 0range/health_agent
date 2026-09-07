from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import openai
import pytest

from health_agent.pilot.contracts import Record
from health_agent.pilot.training import TrainingCoach


class MemoryStore:
    def by_source(self, profile_id, domain, kind, source_key):
        return next((record for owner, record in self.records
                     if owner == profile_id and record.domain == domain
                     and record.kind == kind and record.source_key == source_key), None)

    def __init__(self) -> None:
        self.records: list[tuple[UUID, Record]] = []

    def put(self, profile_id, domain, kind, source_key, payload, *, at=None):
        for owner, record in self.records:
            if owner == profile_id and (
                record.domain,
                record.kind,
                record.source_key,
            ) == (domain, kind, source_key):
                return record
        record = Record(
            f"r{len(self.records)}",
            domain,
            kind,
            source_key,
            at or datetime.now(UTC),
            payload,
        )
        self.records.append((profile_id, record))
        return record

    def list(self, profile_id, domain, kind=None, *, limit=100):
        found = [
            r
            for owner, r in self.records
            if owner == profile_id
            and r.domain == domain
            and (kind is None or r.kind == kind)
        ]
        return sorted(found, key=lambda r: r.at, reverse=True)[:limit]

    def get(self, profile_id, record_id):
        return next(
            (
                r
                for owner, r in self.records
                if owner == profile_id and r.id == record_id
            ),
            None,
        )

    def patch(self, profile_id, record_id, payload):
        for index, (owner, record) in enumerate(self.records):
            if owner == profile_id and record.id == record_id:
                updated = replace(record, payload=payload)
                self.records[index] = (owner, updated)
                return updated
        raise KeyError(record_id)


class Brain:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, system, payload, *, image_path=None):
        self.calls.append((system, payload))
        if payload.get("task") == "weekly_plan":
            return (
                "Черновик: две лёгкие тренировки и день отдыха. Что обычно тренируете?"
            )
        if payload.get("task") == "reflection":
            return "Факт: выполнена одна пробежка. Данных недостаточно, чтобы считать остальные занятия пропущенными."
        if payload.get("task") == "revise_weekly_plan":
            return "Обновлённый черновик: плавание перенесено на четверг."
        return "Помню контекст и отвечаю по тренировкам."


def test_goal_proposal_acceptance_activities_and_reflection_are_durable():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    store.put(
        profile,
        "shared",
        "goal",
        "annual",
        {
            "domain": "training",
            "text": "Бегать регулярно",
            "events": [{"name": "осенний старт", "date": "примерно осенью"}],
        },
        at=now,
    )
    activities = lambda *_: [
        {
            "id": "a1",
            "sport": "run",
            "started_at": "2026-09-06T08:00:00Z",
            "duration_s": 1800,
        }
    ]
    coach = TrainingCoach(store, brain, activity_source=activities)

    assert "Бегать регулярно" in coach.handle(profile, "/год", source_key="g1", now=now)
    proposal = coach.handle(profile, "/план", source_key="p1", now=now)
    assert proposal and not store.list(profile, "training", "accepted_plan")
    assert coach.handle(profile, "/сохранить план", source_key="p2", now=now)
    assert (
        store.list(profile, "training", "accepted_plan")[0].payload["text"] == proposal
    )
    summary = coach.handle(profile, "/итоги", source_key="p3", now=now)
    assert "пробежка" in summary
    assert store.list(profile, "training", "activity")

    restarted = TrainingCoach(store, brain, activity_source=activities)
    assert restarted.handle(
        profile, "/сохранить план", source_key="p2", now=now
    ) == coach.handle(profile, "/сохранить план", source_key="p2", now=now)
    assert len(store.list(profile, "training", "accepted_plan")) == 1


def test_profiles_are_isolated_and_missing_data_is_explicit():
    store, brain, first, second = MemoryStore(), Brain(), uuid4(), uuid4()
    now = datetime(2026, 9, 13, 15, tzinfo=UTC)  # 18:00 Europe/Moscow
    coach = TrainingCoach(store, brain)
    coach.handle(first, "Болит колено после бега", source_key="m1", now=now)
    coach.handle(first, "/план", source_key="p1", now=now)
    coach.handle(first, "/сохранить план", source_key="a1", now=now)

    calls_before = len(brain.calls)
    result = coach.handle(second, "/итоги", source_key="i2", now=now)
    assert "нет данных" in result.lower()
    assert len(brain.calls) == calls_before
    assert not store.list(second, "training", "conversation")
    notice = coach.due(first, now)
    assert len(notice) == 1 and "данн" in notice[0].text.lower()
    store.put(
        first, "training", "notice", notice[0].key, {"text": notice[0].text}, at=now
    )
    assert coach.due(first, now) == []


def test_weekly_due_uses_moscow_time_and_training_delivery_ledger():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = TrainingCoach(store, brain)
    store.put(
        profile,
        "training",
        "accepted_plan",
        "accepted",
        {"text": "draft"},
        at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert coach.due(profile, datetime(2026, 9, 13, 14, 59, tzinfo=UTC)) == []
    now = datetime(2026, 9, 13, 15, tzinfo=UTC)
    notice = coach.due(profile, now)[0]
    store.put(profile, "shared", "notice", notice.key, {"text": notice.text}, at=now)
    assert coach.due(profile, now)  # wrong domain is not a successful delivery ledger
    store.put(profile, "training", "notice", notice.key, {"text": notice.text}, at=now)
    assert coach.due(profile, now) == []


def test_only_explicit_training_goals_are_used_and_every_prompt_has_medical_boundary():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    store.put(
        profile, "shared", "goal", "food", {"text": "Unrelated food goal"}, at=now
    )
    store.put(
        profile,
        "shared",
        "goal",
        "run",
        {"domain": "training", "text": "Training goal"},
        at=now,
    )
    coach = TrainingCoach(store, brain)

    yearly = coach.handle(profile, "/год", source_key="g", now=now)
    assert "Training goal" in yearly and "Unrelated" not in yearly
    coach.handle(
        profile,
        "Назначь лечение боли и поставь медицинскую цель",
        source_key="s",
        now=now,
    )
    system, _ = brain.calls[-1]
    assert "Never diagnose" in system
    assert "treatment" in system
    assert "medical targets" in system


def test_sparse_plan_is_normalized_to_one_final_question():
    store, profile = MemoryStore(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)

    def brain(system, payload, *, image_path=None):
        return "Можно бегать? Или ходить?"

    answer = TrainingCoach(store, brain).handle(
        profile, "/план", source_key="p", now=now
    )
    assert answer.count("?") == 1
    assert answer.endswith("?")


def test_free_dialogue_replays_without_second_model_call_and_remembers_context():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    coach = TrainingCoach(store, brain)
    first = coach.handle(profile, "Хочу снова начать бегать", source_key="m1", now=now)
    calls = len(brain.calls)
    assert (
        coach.handle(profile, "Хочу снова начать бегать", source_key="m1", now=now)
        == first
    )
    assert len(brain.calls) == calls
    coach.handle(
        profile, "Что мы решили?", source_key="m2", now=now + timedelta(minutes=1)
    )
    assert "Хочу снова начать бегать" in str(brain.calls[-1])


def test_plan_uses_bounded_dialogue_and_dialogue_sees_plan_without_accepting_it():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    coach = TrainingCoach(store, brain)
    coach.handle(profile, "На этой неделе только плавание", source_key="m1", now=now)
    proposal = coach.handle(profile, "/план", source_key="p1", now=now)
    plan_payload = brain.calls[-1][1]
    assert "только плавание" in str(plan_payload["recent_training_messages"])
    assert not store.list(profile, "training", "accepted_plan")

    coach.handle(profile, "Перенеси занятие в этом плане", source_key="m2", now=now)
    dialogue_payload = brain.calls[-1][1]
    assert dialogue_payload["latest_proposal"]["text"] == proposal
    assert dialogue_payload["latest_accepted_plan"] is None

    coach.handle(profile, "/сохранить план", source_key="a1", now=now)
    coach.handle(profile, "Что в принятом плане?", source_key="m3", now=now)
    assert brain.calls[-1][1]["latest_accepted_plan"]["text"] == proposal


@pytest.mark.parametrize(
    "failure",
    [
        openai.APITimeoutError(
            request=httpx.Request("POST", "https://test.invalid")  # type: ignore[arg-type]
        ),
        openai.RateLimitError(
            "limited",
            response=httpx.Response(  # type: ignore[arg-type]
                429, request=httpx.Request("POST", "https://test.invalid")
            ),
            body=None,
        ),
    ],
)
def test_real_provider_failures_fall_back_for_plan_dialogue_and_cached_due(failure):
    store, profile = MemoryStore(), uuid4()
    now = datetime(2026, 9, 13, 15, tzinfo=UTC)

    def failing_brain(*args, **kwargs):
        raise failure

    coach = TrainingCoach(store, failing_brain)
    assert "Черновик" in coach.handle(profile, "/план", source_key="p", now=now)
    assert "сохранено" in coach.handle(profile, "Могу плавать", source_key="m", now=now)
    coach.handle(profile, "/сохранить план", source_key="a", now=now)
    first = coach.due(profile, now)
    second = coach.due(profile, now)
    assert first == second
    assert len(store.list(profile, "training", "reflection")) == 1


def test_date_only_interactive_activity_uses_canonical_moscow_day_precision():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    coach = TrainingCoach(
        store, brain, activity_source=lambda *_: [{"id": "day", "date": "2026-09-06"}]
    )
    coach.handle(profile, "/план", source_key="p", now=now)
    activity = store.list(profile, "training", "activity")[0]
    assert activity.at.isoformat() == "2026-09-06T00:00:00+03:00"
    assert activity.payload["timestamp_precision"] == "day"


def _preferences(store, profile, now, *, discipline="плавание", count=2):
    store.put(
        profile,
        "training",
        "settings",
        "weekly-preferences",
        {
            "sessions": [{"discipline": discipline, "count": count}],
            "strength_beginner": True,
            "source": "user",
        },
        at=now,
    )


@pytest.mark.parametrize("offline", [False, True])
def test_revision_preserves_sunday_dates_and_accepted_bytes_on_monday(offline):
    import json

    store, brain, profile = MemoryStore(), Brain(), uuid4()
    sunday = datetime(2026, 9, 6, 15, tzinfo=UTC)
    coach = TrainingCoach(store, brain)
    coach.handle(profile, "/план", source_key="draft", now=sunday)
    coach.handle(profile, "/сохранить план", source_key="accept", now=sunday)
    accepted = store.list(profile, "training", "accepted_plan")[0]
    before = json.dumps(accepted.payload, sort_keys=True)
    if offline:
        def unavailable(*args, **kwargs):
            raise RuntimeError("offline")
        coach = TrainingCoach(store, unavailable)
    coach.handle(profile, "перенеси тренировку на четверг", source_key="revision",
                 now=sunday + timedelta(days=1))
    revised = store.list(profile, "training", "proposal")[0]
    assert revised.payload["date_range"] == {"monday": "2026-09-07", "sunday": "2026-09-13"}
    if not offline:
        assert brain.calls[-1][1]["next_week"] == revised.payload["date_range"]
    assert json.dumps(store.get(profile, accepted.id).payload, sort_keys=True) == before


@pytest.mark.parametrize("text", ["перенеси бассейн", "замени бег", "поменяй велосипед"])
def test_named_discipline_revision_versions_without_acceptance(text):
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 6, 15, tzinfo=UTC)
    coach = TrainingCoach(store, brain)
    coach.handle(profile, "/план", source_key="draft", now=now)
    for index, discussion in enumerate(["почему бассейн во вторник?", "можно перенести бассейн?", "в среду не могу"]):
        coach.handle(profile, discussion, source_key=f"discussion{index}", now=now)
        assert brain.calls[-1][1]["task"] == "dialogue"
    coach.handle(profile, text, source_key="revision", now=now + timedelta(minutes=1))
    assert brain.calls[-1][1]["task"] == "revise_weekly_plan"
    assert len(store.list(profile, "training", "proposal")) == 2
    assert not store.list(profile, "training", "accepted_plan")


def test_long_weekly_sections_both_survive_notice_budget():
    store, profile = MemoryStore(), uuid4()
    now = datetime(2026, 9, 6, 15, tzinfo=UTC)
    store.put(profile, "training", "activity", "run", {"sport": "run"}, at=now)
    def long_brain(system, payload, **kwargs):
        return ("Фактический разбор. " if payload["task"] == "reflection" else "Черновик: плавание. ") * 200
    notice = TrainingCoach(store, long_brain).due(profile, now)[0]
    assert len(notice.text) <= 1800
    assert "Фактический разбор." in notice.text
    assert "Ориентир на следующую неделю, не обязательство" in notice.text
    assert "Черновик: плавание." in notice.text


@pytest.mark.parametrize("dates", [None, {}, {"monday": "invalid", "sunday": "2026-09-13"},
                                  {"monday": "2026-09-08", "sunday": "2026-09-14"},
                                  {"monday": "2026-09-07", "sunday": "2026-09-20"}])
def test_revision_does_not_invent_new_dates_for_invalid_existing_range(dates):
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    store.put(profile, "training", "proposal", "old", {"text": "Черновик", "date_range": dates}, at=now)
    reply = TrainingCoach(store, brain).handle(profile, "перенеси тренировку", source_key="revision", now=now)
    assert "/план" in reply
    assert not brain.calls
    assert len(store.list(profile, "training", "proposal")) == 1


def test_sunday_preferences_prepare_dated_cached_draft_without_acceptance():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 13, 15, tzinfo=UTC)
    _preferences(store, profile, now)
    coach = TrainingCoach(store, brain)

    first = coach.due(profile, now)
    calls = len(brain.calls)
    second = coach.due(profile, now)

    assert first == second and len(first) == 1
    assert len(brain.calls) == calls == 1
    assert "Ориентир на следующую неделю, не обязательство" in first[0].text
    assert not store.list(profile, "training", "accepted_plan")
    proposal = store.list(profile, "training", "proposal")[0]
    assert proposal.source_key == "training-draft-2026-W38"
    assert proposal.payload["date_range"] == {
        "monday": "2026-09-14",
        "sunday": "2026-09-20",
    }
    prompt = brain.calls[0][1]
    assert prompt["weekly_preferences"]["sessions"][0]["discipline"] == "плавание"
    assert prompt["next_week"] == proposal.payload["date_range"]


def test_monday_manual_draft_uses_following_monday_through_sunday():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    monday = datetime(2026, 9, 14, 6, tzinfo=UTC)
    _preferences(store, profile, monday)
    TrainingCoach(store, brain).handle(profile, "/план", source_key="p", now=monday)
    assert brain.calls[-1][1]["next_week"] == {
        "monday": "2026-09-21",
        "sunday": "2026-09-27",
    }


def test_sunday_combines_actual_reflection_and_draft_in_one_notice_then_delivery_stops():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 13, 15, tzinfo=UTC)
    _preferences(store, profile, now)
    store.put(
        profile,
        "training",
        "activity",
        "activity:one",
        {"sport": "run"},
        at=now - timedelta(days=1),
    )
    coach = TrainingCoach(store, brain)
    notices = coach.due(profile, now)
    assert len(notices) == 1
    assert "Факт: выполнена одна пробежка" in notices[0].text
    assert "Ориентир на следующую неделю" in notices[0].text
    assert len(notices[0].text) <= 1800
    store.put(profile, "training", "notice", notices[0].key, {}, at=now)
    assert coach.due(profile, now) == []


def test_weekly_draft_is_profile_scoped_and_monday_is_silent():
    store, brain, first, second = MemoryStore(), Brain(), uuid4(), uuid4()
    sunday = datetime(2026, 9, 13, 15, tzinfo=UTC)
    _preferences(store, first, sunday, discipline="велосипед", count=3)
    coach = TrainingCoach(store, brain)
    assert coach.due(first, sunday)
    assert coach.due(second, sunday) == []
    assert coach.due(first, sunday + timedelta(days=1)) == []


def test_plain_text_show_discussion_and_explicit_revision_are_distinct_and_replay():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    _preferences(store, profile, now)
    coach = TrainingCoach(store, brain)
    original = coach.handle(
        profile, "давай план на неделю", source_key="draft", now=now
    )
    assert coach.handle(profile, "покажи план", source_key="show", now=now) == original

    coach.handle(
        profile, "почему бег во вторник?", source_key="question", now=now
    )
    assert brain.calls[-1][1]["task"] == "dialogue"
    assert len(store.list(profile, "training", "proposal")) == 1

    revised = coach.handle(
        profile,
        "перенеси занятие в этом плане на четверг",
        source_key="revision",
        now=now + timedelta(minutes=2),
    )
    assert "четверг" in revised
    assert brain.calls[-1][1]["current_proposal"]["text"] == original
    assert len(store.list(profile, "training", "proposal")) == 2
    assert not store.list(profile, "training", "accepted_plan")
    calls = len(brain.calls)
    assert coach.handle(
        profile,
        "перенеси занятие в этом плане на четверг",
        source_key="revision",
        now=now + timedelta(minutes=2),
    ) == revised
    assert len(brain.calls) == calls


def test_provider_failure_uses_desired_slots_and_reuses_weekly_draft():
    store, profile = MemoryStore(), uuid4()
    now = datetime(2026, 9, 13, 15, tzinfo=UTC)
    _preferences(store, profile, now, discipline="силовая", count=2)
    calls = 0

    def failing_brain(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise openai.APITimeoutError(
            request=httpx.Request("POST", "https://test.invalid")
        )

    coach = TrainingCoach(store, failing_brain)
    notice = coach.due(profile, now)[0]
    assert "силовая: 2 желаемых слота" in notice.text
    assert "выполн" not in notice.text.lower()
    assert coach.due(profile, now)[0].text == notice.text
    assert calls == 1
