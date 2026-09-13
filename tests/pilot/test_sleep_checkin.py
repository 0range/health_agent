from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_sleep import FakeBrain, MemoryStore

from health_agent.pilot.sleep import SleepCoach
from health_agent.pilot.sleep_checkin import PROMPT, RESTED, keyboard

NOW = datetime(2026, 9, 14, 5, tzinfo=UTC)


def test_two_taps_restart_duplicate_and_optional_comment():
    store, profile = MemoryStore(), uuid4()
    coach = SleepCoach(store, FakeBrain())
    assert not coach.due(profile, NOW - timedelta(seconds=1))
    assert coach.due(profile, NOW)[0].text == PROMPT
    assert coach.handle(profile, "Сон: 7", source_key="a", now=NOW) == RESTED
    restarted = SleepCoach(store, FakeBrain())
    assert restarted.handle(profile, "Сон: 7", source_key="double", now=NOW) == RESTED
    reply = restarted.handle(profile, "Выспался: 6", source_key="b", now=NOW)
    assert reply.startswith("Оценки сна сохранены.")
    assert keyboard(reply) == {"remove_keyboard": True}
    entries = store.list(profile, "sleep", "diary")
    assert len(entries) == 1
    values = entries[0].payload["checkin"]
    assert values["quality_0_10"] == 7 and values["rested_0_10"] == 6
    assert values["wake_date"] == "2026-09-14"
    assert "latency_minutes" not in values and "awakening_count" not in values
    assert restarted.handle(profile, "Выспался: 6", source_key="b", now=NOW) == reply
    assert len(store.list(profile, "sleep", "diary")) == 1
    assert not restarted.due(profile, NOW)


def test_skip_profile_isolation_and_next_day():
    store, profile, other = MemoryStore(), uuid4(), uuid4()
    coach = SleepCoach(store, FakeBrain())
    coach.handle(profile, "/опрос", source_key="start", now=NOW)
    assert "сегодняшнего опроса" in coach.handle(
        other, "Сон: 5", source_key="bad", now=NOW
    )
    coach.handle(profile, "Сон: пропустить", source_key="skip1", now=NOW)
    coach.handle(profile, "Выспался: пропустить", source_key="skip2", now=NOW)
    values = store.list(profile, "sleep", "diary")[0].payload["checkin"]
    assert values["quality_0_10"] is None and values["rested_0_10"] is None
    assert coach.due(profile, NOW + timedelta(days=1))[0].text == PROMPT
    assert len(keyboard(PROMPT)["keyboard"]) == 4


def test_finished_draft_recovers_diary_after_crash():
    store, profile = MemoryStore(), uuid4()
    store.put(
        profile,
        "sleep",
        "checkin",
        "2026-09-14",
        {
            "wake_date": "2026-09-14",
            "schema_version": 2,
            "quality_0_10": 8,
            "rested_0_10": 9,
        },
        at=NOW,
    )
    SleepCoach(store, FakeBrain()).handle(
        profile, "/опрос", source_key="recover", now=NOW
    )
    assert len(store.list(profile, "sleep", "diary")) == 1


def test_telegram_serializes_reply_buttons_and_removes_them_after_completion():
    import json

    import httpx

    from health_agent.pilot.runtime import SleepTelegramAPI

    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    api = SleepTelegramAPI(
        "123:synthetic", client=httpx.Client(transport=httpx.MockTransport(transport))
    )
    api.send_message(1, PROMPT)
    api.send_message(1, RESTED)
    api.send_message(1, "Оценки сна сохранены. Спасибо.")
    assert requests[0]["reply_markup"]["keyboard"][0][0] == "Сон: 0"
    assert requests[1]["reply_markup"]["keyboard"][0][0] == "Выспался: 0"
    assert requests[2]["reply_markup"] == {"remove_keyboard": True}
