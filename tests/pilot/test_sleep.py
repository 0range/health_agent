from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from health_agent.pilot.contracts import Attachment, Record
from health_agent.pilot.sleep import SleepCoach


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[UUID, list[Record]] = {}
        self.sequence = 0

    def put(
        self,
        profile_id: UUID,
        domain: str,
        kind: str,
        source_key: str,
        payload: dict[str, Any],
        *,
        at: datetime | None = None,
    ) -> Record:
        records = self.records.setdefault(profile_id, [])
        for record in records:
            if (record.domain, record.kind, record.source_key) == (domain, kind, source_key):
                return record
        self.sequence += 1
        record = Record(str(self.sequence), domain, kind, source_key, at or datetime.now(UTC), payload)
        records.append(record)
        return record

    def list(
        self, profile_id: UUID, domain: str, kind: str | None = None, *, limit: int = 100
    ) -> list[Record]:
        matches = [
            record
            for record in self.records.get(profile_id, [])
            if record.domain == domain and (kind is None or record.kind == kind)
        ]
        return sorted(matches, key=lambda record: (record.at, int(record.id)), reverse=True)[:limit]

    def get(self, profile_id: UUID, record_id: str) -> Record | None:
        return next(
            (record for record in self.records.get(profile_id, []) if record.id == record_id), None
        )

    def patch(self, profile_id: UUID, record_id: str, payload: dict[str, Any]) -> Record:
        old = self.get(profile_id, record_id)
        assert old is not None
        replacement = Record(old.id, old.domain, old.kind, old.source_key, old.at, payload)
        rows = self.records[profile_id]
        rows[rows.index(old)] = replacement
        return replacement


class FakeBrain:
    def __init__(self, reply: str = "Вы отметили тяжёлое пробуждение. Сегодня сверим время сна?") -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any], Path | None]] = []

    def __call__(
        self, system: str, payload: dict[str, Any], *, image_path: Path | None = None
    ) -> str:
        self.calls.append((system, payload, image_path))
        return self.reply


NOW = datetime(2026, 9, 7, 6, 30, tzinfo=UTC)  # 09:30 Moscow


def test_diary_is_durable_continuous_and_retry_is_idempotent() -> None:
    store, brain, profile = MemoryStore(), FakeBrain(), uuid4()
    coach = SleepCoach(store, brain)

    first = coach.handle(profile, "/сон Проснулся разбитым", source_key="u1", now=NOW)
    assert store.list(profile, "sleep", "diary")[0].payload["text"] == "Проснулся разбитым"
    assert first

    coach.handle(profile, "А что мы вчера решили?", source_key="u2", now=NOW)
    assert "Проснулся разбитым" in str(brain.calls[-1][1])
    assert len(store.list(profile, "sleep", "turn")) == 4

    assert coach.handle(profile, "/сон другой текст", source_key="u1", now=NOW) == first
    assert len(store.list(profile, "sleep", "diary")) == 1
    assert len(brain.calls) == 2


def test_morning_schedule_due_window_pause_timezone_and_no_stale_prompt() -> None:
    store, profile = MemoryStore(), uuid4()
    coach = SleepCoach(store, FakeBrain())

    assert coach.due(profile, datetime(2026, 9, 7, 5, 59, tzinfo=UTC)) == []
    notice = coach.due(profile, NOW)[0]
    assert notice.key == "morning:2026-09-07"
    store.put(profile, "sleep", "notice", notice.key, {"text": notice.text}, at=NOW)
    assert coach.due(profile, NOW) == []
    assert coach.due(profile, datetime(2026, 9, 7, 10, 0, tzinfo=UTC)) == []  # after noon

    coach.handle(profile, "/утро 10:30", source_key="schedule", now=NOW)
    assert coach.due(profile, datetime(2026, 9, 8, 7, 29, tzinfo=UTC)) == []
    assert coach.due(profile, datetime(2026, 9, 8, 7, 31, tzinfo=UTC))
    coach.handle(profile, "/утро выкл", source_key="pause", now=NOW)
    assert coach.due(profile, datetime(2026, 9, 9, 8, 0, tzinfo=UTC)) == []


def test_delivered_morning_reply_becomes_diary_but_question_does_not() -> None:
    store, brain, profile = MemoryStore(), FakeBrain(), uuid4()
    coach = SleepCoach(store, brain)
    store.put(profile, "sleep", "notice", "morning:2026-09-07", {"text": "Как спалось?"}, at=NOW)

    coach.handle(profile, "Плохо, снился поезд", source_key="answer", now=NOW)
    diary = store.list(profile, "sleep", "diary")
    assert diary[0].payload["text"] == "Плохо, снился поезд"
    assert diary[0].payload["dream"] == "снился поезд"
    assert diary[0].payload["morning_prompt_key"] == "morning:2026-09-07"

    coach.handle(profile, "Может ли магний влиять на сон?", source_key="question", now=NOW)
    assert len(store.list(profile, "sleep", "diary")) == 1


def test_question_does_not_consume_prompt_and_pre_delivery_message_is_not_reply() -> None:
    store, profile = MemoryStore(), uuid4()
    coach = SleepCoach(store, FakeBrain())
    delivered = datetime(2026, 9, 7, 6, 20, tzinfo=UTC)
    store.put(
        profile,
        "sleep",
        "notice",
        "morning:2026-09-07",
        {"text": "Как спалось?", "delivered_at": delivered.isoformat()},
        at=delivered,
    )

    coach.handle(
        profile,
        "Стоит ли пить мелатонин?",
        source_key="medical",
        now=datetime(2026, 9, 7, 6, 25, tzinfo=UTC),
    )
    coach.handle(
        profile,
        "Спал плохо",
        source_key="delayed",
        now=datetime(2026, 9, 7, 6, 10, tzinfo=UTC),
    )
    assert store.list(profile, "sleep", "diary") == []

    coach.handle(
        profile,
        "Спал плохо",
        source_key="answer",
        now=datetime(2026, 9, 7, 6, 30, tzinfo=UTC),
    )
    assert store.list(profile, "sleep", "diary")[0].source_key == "answer"


def test_unrelated_statements_and_late_checkin_do_not_consume_morning_prompt() -> None:
    store, profile = MemoryStore(), uuid4()
    coach = SleepCoach(store, FakeBrain())
    delivered = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    store.put(
        profile,
        "sleep",
        "notice",
        "morning:2026-09-07",
        {"delivered_at": delivered.isoformat()},
        at=delivered - timedelta(minutes=5),
    )

    coach.handle(profile, "Сегодня принимаю магний", source_key="magnesium", now=delivered)
    coach.handle(profile, "У меня болит колено", source_key="knee", now=delivered)
    coach.handle(
        profile,
        "Спал плохо",
        source_key="too-late",
        now=delivered + timedelta(hours=3, seconds=1),
    )
    assert store.list(profile, "sleep", "diary") == []

    coach.handle(
        profile,
        "Чувствую себя разбитым",
        source_key="wellbeing",
        now=delivered + timedelta(hours=3),
    )
    assert store.list(profile, "sleep", "diary")[0].source_key == "wellbeing"


def test_read_only_diary_profile_isolation_and_missing_voice_transcription() -> None:
    store, profile, other = MemoryStore(), uuid4(), uuid4()
    coach = SleepCoach(store, FakeBrain())
    store.put(other, "sleep", "diary", "x", {"text": "чужая запись"}, at=NOW)

    assert "пока нет" in coach.handle(profile, "/дневник", source_key="read", now=NOW).lower()
    reply = coach.handle(
        profile,
        "",
        source_key="voice",
        now=NOW,
        attachment=Attachment(Path("voice.ogg"), "audio/ogg"),
    )
    assert "расшифров" in reply.lower()
    assert store.list(profile, "sleep", "diary") == []


def test_weekly_notice_needs_three_entries_and_on_demand_summary_is_truthful() -> None:
    store, profile = MemoryStore(), uuid4()
    coach = SleepCoach(store, FakeBrain("Краткий разбор трёх записей."))
    sunday = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)  # Sunday 19:00 Moscow
    for index in range(2):
        store.put(profile, "sleep", "diary", f"d{index}", {"text": f"сон {index}"}, at=NOW)
    assert coach.due(profile, sunday) == []
    store.put(profile, "sleep", "diary", "d2", {"text": "сон 2"}, at=sunday)
    assert coach.due(profile, sunday)[0].key == "weekly:2026-09-13"
    summary = coach.handle(profile, "/итоги", source_key="with-data", now=sunday)
    assert summary.startswith("За 07.09–13.09: 3 записи.")

    empty_profile = uuid4()
    summary = coach.handle(empty_profile, "/итоги", source_key="summary", now=NOW)
    assert "нет записей" in summary.lower()


def test_weekly_reflection_requires_real_entry_ids_and_labels_hypotheses() -> None:
    store, profile = MemoryStore(), uuid4()
    sunday = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)
    for index in range(3):
        store.put(
            profile,
            "sleep",
            "diary",
            f"d{index}",
            {"text": f"отчёт {index}"},
            at=NOW + timedelta(days=index),
        )
    ids = [row.id for row in store.list(profile, "sleep", "diary")]
    brain = FakeBrain(
        json.dumps(
            {
                "observations": [{"text": "Три пользовательских отчёта", "entry_ids": ids}],
                "hypotheses": [{"text": "Режим мог иметь значение", "entry_ids": ids[:1]}],
                "next_question": "Что отличало наиболее бодрое утро?",
            },
            ensure_ascii=False,
        )
    )
    reply = SleepCoach(store, brain).handle(
        profile, "/итоги", source_key="structured", now=sunday
    )
    assert "Наблюдение:" in reply
    assert "Гипотеза (не установленная причина):" in reply
    assert "Что отличало" in reply
    weekly_entries = brain.calls[-1][1]["weekly_entries"]
    assert {row["id"] for row in weekly_entries} == set(ids)


def test_weekly_invalid_or_unknown_ids_get_deterministic_useful_fallback() -> None:
    store, profile = MemoryStore(), uuid4()
    sunday = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)
    for index in range(3):
        store.put(profile, "sleep", "diary", f"d{index}", {"text": "сон"}, at=NOW)
    invalid = json.dumps(
        {
            "observations": [{"text": "Выдуманная связь", "entry_ids": ["unknown"]}],
            "hypotheses": [],
            "next_question": "",
        }
    )
    reply = SleepCoach(store, FakeBrain(invalid)).handle(
        profile, "/итоги", source_key="invalid", now=sunday
    )
    assert reply.startswith("За 07.09–07.09: 3 записи.")
    assert "не удалось связать" in reply.lower()
    assert "Что отличало" in reply
    assert "Выдуманная связь" not in reply


def test_health_context_and_goals_are_context_not_claims_and_fallback_saves() -> None:
    store, profile = MemoryStore(), uuid4()
    store.put(profile, "shared", "goal", "g1", {"text": "ложиться раньше"}, at=NOW)

    def unavailable(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("provider down")

    coach = SleepCoach(store, unavailable, health_context=lambda _p, _t: {"whoop": "missing"})
    reply = coach.handle(profile, "/сон Спал семь часов", source_key="entry", now=NOW)
    assert "сохран" in reply.lower()
    assert "09:00" in reply
    assert store.list(profile, "sleep", "diary")


def test_exact_sleep_command_invalid_afternoon_schedule_and_control_replay() -> None:
    store, brain, profile = MemoryStore(), FakeBrain(), uuid4()
    coach = SleepCoach(store, brain)

    coach.handle(profile, "/сонник — это про сны?", source_key="not-command", now=NOW)
    assert store.list(profile, "sleep", "diary") == []
    assert "утрен" in coach.handle(profile, "/утро 12:00", source_key="late", now=NOW).lower()
    assert store.list(profile, "sleep", "schedule") == []

    first = coach.handle(profile, "/утро 10:30", source_key="same", now=NOW)
    replay = coach.handle(profile, "/утро выкл", source_key="same", now=NOW)
    assert replay == first
    assert coach.due(profile, datetime(2026, 9, 8, 7, 31, tzinfo=UTC))


def test_retry_rebuilds_request_from_durable_user_turn() -> None:
    store, brain, profile = MemoryStore(), FakeBrain(), uuid4()
    coach = SleepCoach(store, brain)
    store.put(
        profile,
        "sleep",
        "turn",
        "user:retry",
        {"role": "user", "text": "/сон исходный текст"},
        at=NOW,
    )
    store.put(
        profile,
        "sleep",
        "diary",
        "retry",
        {"text": "исходный текст"},
        at=NOW,
    )

    coach.handle(profile, "/сон изменённый текст", source_key="retry", now=NOW)
    assert brain.calls[-1][1]["request"] == "/сон исходный текст"
    assert store.list(profile, "sleep", "diary")[0].payload["text"] == "исходный текст"


def test_prompt_safety_labels_and_phone_limit() -> None:
    store, profile = MemoryStore(), uuid4()
    brain = FakeBrain("x" * 1500)
    reply = SleepCoach(store, brain).handle(
        profile, "/сон Проснулся с головной болью", source_key="safe", now=NOW
    )
    system, payload, _ = brain.calls[-1]
    assert len(reply) == 1200
    assert "не диагнозы" in system
    assert "diary_user_reports" in payload
    assert payload["diary_user_reports"][0]["text"] == "Проснулся с головной болью"
