"""Cloud opening alerts; the existing ChatGPT task owns daily/weekly digests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from health_agent.races.sources import Observation, collect, official_url

MOSCOW = ZoneInfo("Europe/Moscow")
RELAY = "https://telegram-relay-production-ede9d9fc.up.railway.app"
CHANNELS = ("runc_run", "wn_marathon", "ironstar_official")
MONTHS = dict(
    zip(
        [
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ],
        range(1, 13),
        strict=True,
    )
)
DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(20\d{2}))?\b")
CLOCK = re.compile(r"\b(?:в|с)\s*(\d{1,2})[:.](\d{2})\b")
OPENING = re.compile(
    r"регистрац\w*[^.!?\n]{0,100}(?:откро|открыва|старт)|(?:откро|открыва|старт)[^.!?\n]{0,100}регистрац",
    re.IGNORECASE,
)


def source_allowed(url: str) -> bool:
    p = urlparse(url)
    return official_url(url) or (
        p.scheme == "https"
        and p.hostname == "t.me"
        and any(re.fullmatch(rf"/(?:s/)?{c}(?:/\d+)?", p.path) for c in CHANNELS)
    )


def cadence(now: datetime, windows: dict, announced: dict) -> int:
    local = now.astimezone(MOSCOW)
    for item in announced.values():
        opening = datetime.fromisoformat(item["opens_at"])
        if opening - timedelta(minutes=30) <= local <= opening + timedelta(hours=24):
            return 300
    for item in announced.values():
        opening = datetime.fromisoformat(item["opens_at"])
        if opening - timedelta(days=7) <= local < opening:
            return 3600
    for item in windows["events"]:
        start = datetime.fromisoformat(item["start"]).date() - timedelta(days=7)
        end = datetime.fromisoformat(item["end"]).date() + timedelta(days=14)
        if start <= local.date() <= end:
            return 3600
    return 86400


def validate_opening(data: dict, windows: dict, now: datetime) -> dict:
    if not isinstance(data, dict):
        raise ValueError("invalid_opening")
    if data.get("event_id") not in {e["id"] for e in windows["events"]}:
        raise ValueError("unknown_event")
    if not isinstance(data.get("source"), str) or not source_allowed(data["source"]):
        raise ValueError("unapproved_source")
    opening = datetime.fromisoformat(data.get("opens_at", ""))
    if opening.tzinfo is None or not now - timedelta(
        hours=24
    ) <= opening <= now + timedelta(days=400):
        raise ValueError("invalid_opening_time")
    if opening.astimezone(MOSCOW).year not in (windows["year"] - 1, windows["year"]):
        raise ValueError("wrong_season")
    return {
        "opens_at": opening.astimezone(MOSCOW).isoformat(),
        "source": data["source"],
    }


def parse_announcement(
    content: str, published: datetime, now: datetime, windows: dict, source: str
) -> dict:
    """Only explicit near-future dates/times; ambiguous posts remain for AI review."""
    if not source_allowed(source) or not now - timedelta(days=14) <= published <= now:
        return {}
    text = content.lower().replace("ё", "е")
    if not OPENING.search(text):
        return {}
    # Explicit prior-season mentions must not activate a future-season timer.
    seasons = set(re.findall(r"\b20\d{2}\b", text))
    if seasons and str(windows["year"]) not in seasons:
        return {}
    dates = list(DATE.finditer(text))
    clocks = list(CLOCK.finditer(text))
    if len(dates) != 1 or len(clocks) != 1:
        return {}
    d, c = dates[0], clocks[0]
    if abs(d.start() - c.start()) > 100:
        return {}
    try:
        year = int(d[3]) if d[3] else published.astimezone(MOSCOW).year
        opening = datetime(
            year, MONTHS[d[2]], int(d[1]), int(c[1]), int(c[2]), tzinfo=MOSCOW
        )
        if (
            not d[3]
            and opening < published - timedelta(days=1)
            and published.month == 12
        ):
            opening = opening.replace(year=year + 1)
        if not now - timedelta(hours=24) <= opening <= now + timedelta(days=30):
            return {}
    except ValueError:
        return {}
    result = {}
    for event in windows["events"]:
        if any(alias in text for alias in event["aliases"]):
            result[event["id"]] = {"opens_at": opening.isoformat(), "source": source}
    return result


def alert(obs: Observation, year: int) -> tuple[str, str]:
    path = urlparse(obs.registration_url).path
    if (
        obs.status not in ("open", "verify_distance")
        or not official_url(obs.registration_url)
        or path in ("", "/", "/event", "/event/")
    ):
        raise ValueError("confirmed_registration_link_required")
    suffix = hashlib.sha256(obs.key.encode()).hexdigest()[:24]
    uncertain = obs.status == "verify_distance"
    kind = "check" if uncertain else "open"
    title = "Появилась регистрация на событие" if uncertain else "Открылась регистрация"
    note = (
        "Доступность именно этой дистанции нужно проверить в личном кабинете."
        if uncertain
        else "Места могут закончиться быстро."
    )
    return (
        f"races-{kind}-{year}-{suffix}",
        (
            f"{title} · сезон {year}\n{obs.name}\n{obs.date}\n"
            f"Записаться: {obs.registration_url}\n{note}"
        ),
    )


def fetch(client: httpx.Client, url: str) -> str:
    # Validate every redirect before issuing it; never send credentials to sources.
    for _ in range(5):
        if not source_allowed(url):
            raise ValueError("unapproved_source")
        for attempt in range(2):
            try:
                response = client.get(url, follow_redirects=False)
                break
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt:
                    raise
        if response.is_redirect:
            from urllib.parse import urljoin

            url = urljoin(url, response.headers["location"])
            continue
        response.raise_for_status()
        if len(response.content) > 8_000_000:
            raise ValueError("source_too_large")
        return response.text
    raise ValueError("too_many_redirects")


def scan(
    config: dict, windows: dict, now: datetime
) -> tuple[list[Observation], list[str], dict]:
    pages = {}
    with httpx.Client(
        timeout=httpx.Timeout(20, connect=8),
        headers={"User-Agent": "HealthAgent-Races/2.0", "Accept-Language": "ru-RU"},
    ) as client:

        def get(url):
            value = fetch(client, url)
            pages[url] = value
            return value

        observations, errors = collect(config, get)
        announced = {}
        for channel in CHANNELS:
            try:
                soup = BeautifulSoup(
                    fetch(client, f"https://t.me/s/{channel}"), "html.parser"
                )
                posts = soup.select(".tgme_widget_message")
                if not posts:
                    raise ValueError("announcement_layout_changed")
                for post in posts:
                    content = post.select_one(".tgme_widget_message_text")
                    stamp = post.select_one("time[datetime]")
                    link = post.select_one(".tgme_widget_message_date[href]")
                    if content and stamp and link:
                        announced.update(
                            parse_announcement(
                                content.get_text(" ", strip=True),
                                datetime.fromisoformat(stamp["datetime"]),
                                now,
                                windows,
                                link["href"],
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Анонсы {channel}: {type(exc).__name__}")
        # Event pages without publication dates can contribute explicit near-future
        # season-2027 announcements; the year guard excludes their old 2026 copy.
        for url, html in pages.items():
            soup = BeautifulSoup(html, "html.parser")
            for el in soup.select("script, style, header, footer, nav"):
                el.decompose()
            for node in soup.select("p, .alert, .run-intro"):
                content = node.get_text(" ", strip=True)
                if str(config["year"]) in content:
                    announced.update(
                        parse_announcement(content, now, now, windows, url)
                    )
        return observations, errors, announced


def send(key: str, message: str) -> int:
    secret = os.environ["TRAINING_RELAY_KEY"]
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        result = client.post(
            RELAY + "/notify/training",
            headers={"Authorization": "Bearer " + secret, "Idempotency-Key": key},
            json={"text": message},
        )
        result.raise_for_status()
        data = result.json()
    if (
        data.get("ok") is not True
        or data.get("channel") != "training"
        or type(data.get("message_id")) is not int
        or data["message_id"] <= 0
    ):
        raise ValueError("delivery_unconfirmed")
    return data["message_id"]


class Worker:
    def __init__(self, config: dict, windows: dict, root: Path, *, sender=send):
        self.config, self.windows, self.root, self.sender = (
            config,
            windows,
            root,
            sender,
        )
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.state.setdefault("outbox", {})
        self.state.setdefault("announced", {})
        self.lock = threading.RLock()
        self.last_loop = time.monotonic()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def record(self, observations, errors, announced, now):
        with self.lock:
            self.state.update(checked_at=now.isoformat(), errors=errors)
            self.state.setdefault("observations", {}).update(
                {
                    o.key: asdict(o) | {"checked_at": now.isoformat()}
                    for o in observations
                }
            )
            self.state["announced"].update(announced)
            for obs in observations:
                if obs.status in ("open", "verify_distance"):
                    key, message = alert(obs, self.windows["year"])
                    self.state["outbox"].setdefault(
                        key,
                        {
                            "text": message,
                            "observation_key": obs.key,
                            "created_at": now.isoformat(),
                        },
                    )
            self.save()

    def deliver(self):
        with self.lock:
            pending = [
                (key, dict(value))
                for key, value in self.state["outbox"].items()
                if not value.get("message_id")
            ]
        for key, item in pending:
            try:
                message_id = self.sender(key, item["text"])
                with self.lock:
                    self.state["outbox"][key].update(
                        message_id=message_id, sent_at=datetime.now(MOSCOW).isoformat()
                    )
                    self.state["outbox"][key].pop("delivery_error", None)
                    self.save()
            except Exception as exc:  # noqa: BLE001 -- never log HTTP exceptions containing credentials
                with self.lock:
                    self.state["outbox"][key]["delivery_error"] = type(exc).__name__
                    self.save()

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.state)) | {
                "year": self.windows["year"],
                "cadence_seconds": cadence(
                    datetime.now(MOSCOW), self.windows, self.state["announced"]
                ),
                "event_ids": [e["id"] for e in self.windows["events"]],
            }

    def loop(self):
        next_delivery = 0.0
        while True:
            self.last_loop = time.monotonic()
            now = datetime.now(MOSCOW)
            try:
                with self.lock:
                    previous = self.state.get("checked_at")
                    period = cadence(now, self.windows, self.state["announced"])
                active = now.date().isoformat() <= f"{self.windows['year']}-12-31"
                if active and (
                    not previous
                    or (now - datetime.fromisoformat(previous)).total_seconds()
                    >= period
                ):
                    observations, errors, announced = scan(
                        self.config, self.windows, now
                    )
                    self.record(observations, errors, announced, now)
                    print(
                        json.dumps(
                            {
                                "event": "race_check",
                                "checked_at": now.isoformat(),
                                "observations": len(observations),
                                "errors": errors,
                                "cadence_seconds": period,
                            }
                        ),
                        flush=True,
                    )
                    next_delivery = 0
                if time.monotonic() >= next_delivery:
                    self.deliver()
                    next_delivery = time.monotonic() + 300
            except Exception as exc:  # noqa: BLE001
                print(
                    json.dumps({"event": "worker_error", "type": type(exc).__name__}),
                    flush=True,
                )
            time.sleep(15)


def handler_for(worker: Worker, secret: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data):
            payload = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            return len(secret) >= 32 and hmac.compare_digest(
                self.headers.get("Authorization", "").encode(),
                ("Bearer " + secret).encode(),
            )

        def do_GET(self):
            if self.path == "/healthz":
                healthy = time.monotonic() - worker.last_loop < 600
                return self.reply(200 if healthy else 503, {"ok": healthy})
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            if self.path == "/status":
                return self.reply(200, worker.status())
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            if self.path != "/openings":
                return self.reply(404, {"error": "not_found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < size <= 16384
                    or self.headers.get("Content-Type", "").split(";")[0]
                    != "application/json"
                ):
                    raise ValueError("invalid_body")
                data = json.loads(self.rfile.read(size))
                value = validate_opening(data, worker.windows, datetime.now(MOSCOW))
                with worker.lock:
                    worker.state["announced"][data["event_id"]] = value
                    worker.save()
                return self.reply(200, {"ok": True, "opening": value})
            except (ValueError, TypeError, KeyError):
                return self.reply(400, {"error": "invalid_opening"})

    return Handler


def main():
    secret = os.environ["TRAINING_RELAY_KEY"]
    if len(secret) < 32:
        raise ValueError("missing_training_key")
    config = json.loads(Path("races-2027.json").read_text())
    windows = json.loads(Path("race-windows-2027.json").read_text())
    worker = Worker(config, windows, Path(os.environ.get("RACE_STATE_DIR", "/data")))
    threading.Thread(target=worker.loop, daemon=True).start()
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8080"))), handler_for(worker, secret)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
