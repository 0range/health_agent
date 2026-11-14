from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from health_agent.races.monitor import build_report, due, read_json, run, write_json
from health_agent.races.sources import Observation
from health_agent.telegram.api import TelegramDeferred, TelegramDeliveryUnknown

CONFIG = {"year": 2027}
NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)  # 13:00 Moscow
OBS = Observation(
    "april", "Апрель · 5 км", "https://aprilrun5km.runc.run/", "not_announced"
)


def test_gate_moscow_13_and_catch_up_and_no_duplicates():
    assert not due(NOW - timedelta(seconds=1), {})
    assert due(NOW, {})
    assert due(NOW + timedelta(hours=4), {})
    assert not due(NOW, {"sent_on": "2026-11-15"})
    assert due(NOW + timedelta(days=1), {"sent_on": "2026-11-15"})


def test_unchanged_exact_text_and_errors_never_no_updates():
    previous = {OBS.key: asdict(OBS)}
    assert build_report(previous, [OBS], []) == "нет обновлений"
    message = build_report(previous, [OBS], ["IRONSTAR: TimeoutError"])
    assert "Не удалось проверить" in message
    assert "нет обновлений" not in message


def test_opening_contains_direct_link_and_new_date_is_reported():
    opened = Observation(
        OBS.key,
        OBS.name,
        OBS.url,
        "open",
        "4 апреля 2027",
        "https://runc.run/race/999/",
    )
    report = build_report({OBS.key: asdict(OBS)}, [opened], [])
    assert "Открылась регистрация" in report
    assert opened.registration_url in report
    assert "4 апреля 2027" in report


def test_daily_send_only_once_and_errors_preserve_previous_snapshot(tmp_path):
    sent = []

    def sender(message, key):
        sent.append((message, key))
        return SimpleNamespace(parts=1)

    scanner = lambda _: ([OBS], [])
    assert run(CONFIG, tmp_path, now=NOW, scanner=scanner, sender=sender) == "sent"
    assert run(CONFIG, tmp_path, now=NOW, scanner=scanner, sender=sender) == "not_due"
    assert len(sent) == 1
    assert sent[0][0] == "нет обновлений"
    assert (
        run(
            CONFIG,
            tmp_path,
            now=NOW + timedelta(days=1),
            scanner=lambda _: ([], ["site unavailable"]),
            sender=sender,
        )
        == "sent"
    )
    state = read_json(tmp_path / "state.json")
    assert state["observations"][OBS.key] == asdict(OBS)
    assert "Не удалось проверить" in sent[1][0]


def test_rate_limit_retry_uses_frozen_outbox_without_refetch(tmp_path):
    calls = []

    def throttled(message, key):
        calls.append((message, key))
        raise TelegramDeferred(NOW + timedelta(minutes=5))

    assert (
        run(CONFIG, tmp_path, now=NOW, scanner=lambda _: ([OBS], []), sender=throttled)
        == "delivery_deferred"
    )
    assert (
        run(CONFIG, tmp_path, now=NOW + timedelta(minutes=1), sender=throttled)
        == "deferred"
    )

    def sender(message, key):
        calls.append((message, key))
        return SimpleNamespace(parts=1)

    def no_scan(_):
        raise AssertionError("Retry must not change the report")

    assert (
        run(
            CONFIG,
            tmp_path,
            now=NOW + timedelta(minutes=5),
            scanner=no_scan,
            sender=sender,
        )
        == "sent"
    )
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_ambiguous_send_is_visible_and_does_not_retry_that_day(tmp_path):
    def sender(message, key):
        raise TelegramDeliveryUnknown()

    assert (
        run(CONFIG, tmp_path, now=NOW, scanner=lambda _: ([OBS], []), sender=sender)
        == "delivery_unknown"
    )
    state = read_json(tmp_path / "state.json")
    assert state["delivery_status"] == "unknown"
    assert "sent_on" not in state
    assert (
        run(CONFIG, tmp_path, now=NOW + timedelta(hours=1), sender=sender) == "deferred"
    )


def test_setup_sends_confirmation_and_initial_findings(tmp_path):
    sent = []

    def sender(message, key):
        sent.append(message)
        return SimpleNamespace(parts=1)

    assert (
        run(
            CONFIG,
            tmp_path,
            now=NOW,
            setup=True,
            scanner=lambda _: ([OBS], []),
            sender=sender,
        )
        == "sent"
    )
    assert "13:00 Мск" in sent[0]
    assert "Открытых регистраций" in sent[0]
    assert "Mac" in sent[0]


def test_future_next_attempt_is_respected(tmp_path):
    write_json(
        tmp_path / "state.json",
        {"next_attempt": (NOW + timedelta(minutes=15)).isoformat()},
    )
    assert run(CONFIG, tmp_path, now=NOW) == "deferred"
