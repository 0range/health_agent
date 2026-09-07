from __future__ import annotations

from datetime import UTC, datetime
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

    tomorrow = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
    coach.handle(profile, "Может ли магний влиять на сон?", source_key="question", now=tomorrow)
    assert len(store.list(profile, "sleep", "diary")) == 1


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

    empty_profile = uuid4()
    summary = coach.handle(empty_profile, "/итоги", source_key="summary", now=NOW)
    assert "нет записей" in summary.lower()


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
