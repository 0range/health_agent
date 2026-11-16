import threading
from datetime import datetime
from http.server import ThreadingHTTPServer

import httpx
import pytest

from health_agent.races.cloud import (
    MOSCOW,
    Worker,
    alert,
    alert_card,
    cadence,
    handler_for,
    parse_announcement,
    validate_opening,
)
from health_agent.races.sources import Observation


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=MOSCOW)


WINDOWS = {
    "year": 2027,
    "events": [
        {
            "id": "white_nights",
            "aliases": ["белые ночи"],
            "start": "2026-11-12",
            "end": "2027-03-05",
        }
    ],
}


def test_cadence_includes_previous_calendar_year_and_window_margins():
    assert cadence(at("2026-11-04T12:00"), WINDOWS, {}) == 86400
    assert cadence(at("2026-11-05T00:00"), WINDOWS, {}) == 3600
    assert cadence(at("2027-03-19T23:59"), WINDOWS, {}) == 3600
    assert cadence(at("2027-03-20T00:00"), WINDOWS, {}) == 86400


def test_exact_announcement_overrides_forecast_only_in_bounded_window():
    announced = {"white_nights": {"opens_at": "2027-04-01T14:00:00+03:00"}}
    assert cadence(at("2027-04-01T13:29"), WINDOWS, announced) == 3600
    assert cadence(at("2027-04-01T13:30"), WINDOWS, announced) == 300
    assert cadence(at("2027-04-02T14:00"), WINDOWS, announced) == 300
    assert cadence(at("2027-04-02T14:01"), WINDOWS, announced) == 86400


def test_announcement_needs_explicit_date_time_and_current_post():
    now = at("2026-11-14T18:00")
    text = "Белые ночи 2027: регистрация откроется 20 ноября в 14:00 Мск."
    found = parse_announcement(text, now, now, WINDOWS, "https://t.me/wn_marathon/1500")
    assert found["white_nights"]["opens_at"] == "2026-11-20T14:00:00+03:00"
    assert not parse_announcement(
        text.replace(" в 14:00 Мск", ""),
        now,
        now,
        WINDOWS,
        "https://t.me/wn_marathon/1500",
    )
    assert not parse_announcement(
        text, at("2025-11-14T18:00"), now, WINDOWS, "https://t.me/wn_marathon/1500"
    )
    assert not parse_announcement(
        text.replace("2027", "2026"), now, now, WINDOWS, "https://t.me/wn_marathon/1500"
    )


def test_manual_announcement_requires_known_event_and_official_source():
    now = at("2026-11-14T18:00")
    data = {
        "event_id": "white_nights",
        "opens_at": "2026-11-20T14:00:00+03:00",
        "source": "https://t.me/wn_marathon/1500",
    }
    assert validate_opening(data, WINDOWS, now)["opens_at"] == data["opens_at"]
    for update in (
        {"event_id": "unrelated"},
        {"source": "http://127.0.0.1/"},
        {"opens_at": "2025-01-01T14:00:00+03:00"},
        {"opens_at": "2027-01-01T14:00:00"},
    ):
        with pytest.raises(ValueError):
            validate_opening(data | update, WINDOWS, now)


def test_alert_requires_confirmed_open_and_direct_official_link():
    obs = Observation(
        "runc:april:5",
        "Апрель · 5 км",
        "https://aprilrun5km.runc.run/",
        "open",
        "4 апреля 2027",
        "https://runc.run/race/123/",
    )
    key, message = alert(obs, 2027)
    assert key.startswith("races-open-2027-")
    assert "https://runc.run/race/123/" in message
    assert "4 апреля 2027" in message
    uncertain_key, uncertain_message = alert(
        Observation(
            "x", "x", obs.url, "verify_distance", registration_url=obs.registration_url
        ),
        2027,
    )
    assert uncertain_key.startswith("races-check-")
    assert "Открылась регистрация" not in uncertain_message
    assert "нужно проверить в личном кабинете" in uncertain_message
    with pytest.raises(ValueError):
        alert(
            Observation(
                "x", "x", obs.url, "open", registration_url="https://runc.run/"
            ),
            2027,
        )


def test_restart_preserves_pending_message_and_receipt(tmp_path):
    obs = Observation(
        "runc:april:5",
        "Апрель · 5 км",
        "https://aprilrun5km.runc.run/",
        "open",
        "4 апреля 2027",
        "https://runc.run/race/123/",
    )
    sent = []

    def fail(key, message):
        sent.append((key, message))
        raise TimeoutError()

    def success(key, message):
        sent.append((key, message))
        return 123

    worker = Worker({}, WINDOWS, tmp_path, sender=fail)
    worker.record([obs], [], {}, at("2026-11-14T18:00"))
    worker.deliver()
    worker = Worker({}, WINDOWS, tmp_path, sender=success)
    worker.deliver()
    worker.record([obs], [], {}, at("2026-11-14T19:00"))
    worker.deliver()
    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert next(iter(worker.state["outbox"].values()))["message_id"] == 123


def test_opening_card_is_forwardable_escaped_and_has_direct_registration_link():
    obs = Observation(
        "runc:one:5",
        "Москва <старт> & 5 км",
        "https://runc.run/",
        "open",
        "4 апреля 2027",
        "https://runc.run/race/123/?a=1&b=2",
    )
    key, payload = alert_card(obs, 2027)
    assert key == alert(obs, 2027)[0]
    assert payload["parse_mode"] == "HTML"
    assert "&lt;старт&gt; &amp; 5 км" in payload["text"]
    assert "a=1&amp;b=2" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "Регистрация ↗", "url": obs.registration_url}]
    ]
    assert "protect_content" not in payload


def test_legacy_outbox_keeps_original_payload_on_retry(tmp_path):
    sent = []
    worker = Worker(
        {}, WINDOWS, tmp_path, sender=lambda k, v: sent.append((k, v)) or 123
    )
    worker.state["outbox"]["legacy-key"] = {"text": "original plain text"}
    worker.save()
    worker.deliver()
    assert sent == [("legacy-key", "original plain text")]


def test_authenticated_cloud_status_and_opening_update(tmp_path):
    worker = Worker({}, WINDOWS, tmp_path)
    secret = "test-only-key-" + "a" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(worker, secret))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/status").status_code == 401
            assert client.post("/openings", json={}).status_code == 401
            assert client.post("/check").status_code == 401
            headers = {"Authorization": "Bearer " + secret}
            response = client.get("/status", headers=headers)
            assert response.status_code == 200
            assert secret not in response.text
            assert client.post("/check", headers=headers).status_code == 202
            assert worker.force_check.is_set()
            assert client.post("/openings", headers=headers, json={}).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
