"""Local daily gate and immutable outbox for the training bot only."""

from __future__ import annotations

import argparse
import fcntl
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from health_agent.automation.storage import atomic_private_write, private_directory
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.races.sources import Observation, collect, official_url
from health_agent.telegram.api import (
    TelegramBotAPI,
    TelegramDeferred,
    TelegramDeliveryUnknown,
)
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState

MOSCOW = ZoneInfo("Europe/Moscow")
EXPECTED_BOT = "Training_vitaliy_bot"
STATUS = {
    "not_announced": "страница сезона ещё не опубликована",
    "not_open": "публичная регистрация не открыта",
    "distance_not_listed": "дистанция пока не указана в программе",
    "verify_distance": "появилась регистрация на событие; доступность этой дистанции нужно проверить в личном кабинете",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_json(path: Path, value: dict) -> None:
    atomic_private_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def due(now: datetime, state: dict) -> bool:
    local = now.astimezone(MOSCOW)
    return local.hour >= 13 and state.get("sent_on") != local.date().isoformat()


def build_report(previous: dict, current: list[Observation], errors: list[str]) -> str:
    updates = []
    for obs in current:
        before = previous.get(obs.key)
        if before and all(
            before.get(k, "") == getattr(obs, k)
            for k in ("status", "date", "registration_url")
        ):
            continue
        if before is None and obs.status == "not_announced":
            continue
        if obs.status == "open":
            label = "Открылась регистрация"
        else:
            label = STATUS[obs.status]
        updates.append(
            f"{obs.name}: {label}"
            + (f"\n{obs.date}" if obs.date else "")
            + f"\n{obs.registration_url or obs.url}"
        )
    if errors:
        updates.append("Не удалось проверить:\n" + "\n".join(errors))
    return "\n\n".join(updates) if updates else "нет обновлений"


def fetch_with(client: httpx.Client, url: str) -> str:
    if not official_url(url):
        raise ValueError("unapproved_source")
    for attempt in range(2):
        try:
            response = client.get(url)
            response.raise_for_status()
            if not official_url(str(response.url)):
                raise ValueError("unexpected_redirect")
            if len(response.content) > 8_000_000:
                raise ValueError("source_too_large")
            return response.text
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt:
                raise
    raise RuntimeError("unreachable")


def scan(config: dict) -> tuple[list[Observation], list[str]]:
    with httpx.Client(
        timeout=httpx.Timeout(20, connect=8),
        follow_redirects=True,
        headers={
            "Accept-Language": "ru-RU,ru;q=0.9",
            "User-Agent": "HealthAgent-RaceMonitor/1.0",
        },
    ) as client:
        return collect(config, lambda url: fetch_with(client, url))


def send_training(message: str, delivery_key: str):
    root = Path("data/pilot/training/telegram")
    credential = PrivateBotTokenStore(root / "bot-token").load_verified()
    if credential.username.lower() != EXPECTED_BOT.lower():
        raise ValueError("training_bot_identity_mismatch")
    state = SqliteTelegramState(root / "state.sqlite3")
    with httpx.Client(timeout=httpx.Timeout(10, read=30)) as client:
        gateway = TelegramBotAPI(credential.token, client=client, max_attempts=1)
        messenger = TelegramMessenger(credential.bot_id, gateway, state)
        return messenger.send_to_profile(
            DEFAULT_PROFILE_ID, message, delivery_key=delivery_key
        )


def run(
    config: dict,
    root: Path,
    *,
    now: datetime,
    setup: bool = False,
    scanner=scan,
    sender=send_training,
) -> str:
    private_directory(root)
    with (root / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "already_running"
        state_path = root / "state.json"
        state = read_json(state_path)
        day = now.astimezone(MOSCOW).date().isoformat()
        if state.get("sent_on") == day or (not setup and not due(now, state)):
            return "not_due"
        next_attempt = state.get("next_attempt")
        if next_attempt and now < datetime.fromisoformat(next_attempt):
            return "deferred"
        outbox_path = root / "outbox" / f"{day}.json"
        outbox = read_json(outbox_path)
        if not outbox:
            current, errors = scanner(config)
            report = build_report(state.get("observations", {}), current, errors)
            if setup:
                report = (
                    f"Мониторинг стартов {config['year']} включён. Ежедневно в 13:00 Мск.\n"
                    "Беговое сообщество: все 5 и 10 км. IRONSTAR 1/8: Сочи/Сириус и Лужники.\n"
                    "При отсутствии изменений: «нет обновлений». При открытии регистрации — ссылка.\n"
                    "Проверка работает на Mac; после сна отчёт придёт при пробуждении.\n\n"
                    + (
                        "Открытых регистраций на выбранные старты пока не найдено."
                        if report == "нет обновлений"
                        else report
                    )
                )
            merged = dict(state.get("observations", {}))
            merged.update({obs.key: asdict(obs) for obs in current})
            outbox = {
                "message": report,
                "observations": merged,
                "checked_at": now.isoformat(),
                "errors": errors,
                "delivery_key": f"race-monitor:{config['year']}:{day}",
            }
            write_json(outbox_path, outbox)
        # Outbox content is fixed before the first send, including on retries.
        try:
            delivery = sender(outbox["message"], outbox["delivery_key"])
        except TelegramDeferred as exc:
            state.update(
                next_attempt=exc.retry_at.isoformat(), delivery_status="deferred"
            )
            write_json(state_path, state)
            return "delivery_deferred"
        except TelegramDeliveryUnknown:
            state.update(
                next_attempt=(
                    now.astimezone(MOSCOW).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    + timedelta(days=1)
                ).isoformat(),
                delivery_status="unknown",
                delivery_unknown_on=day,
            )
            write_json(state_path, state)
            return "delivery_unknown"
        except Exception as exc:  # noqa: BLE001 -- never log credentials or upstream payloads
            state.update(
                next_attempt=(now + timedelta(minutes=15)).isoformat(),
                delivery_status="failed",
                error=type(exc).__name__,
            )
            write_json(state_path, state)
            return "delivery_failed"
        state = {
            "sent_on": day,
            "observations": outbox["observations"],
            "checked_at": outbox["checked_at"],
            "errors": outbox["errors"],
            "delivery_status": "sent",
            "delivery_parts": delivery.parts,
        }
        write_json(state_path, state)
        # Public source snapshots and private outboxes are only needed briefly.
        for old in (root / "outbox").glob("*.json"):
            if (
                old.name
                < (now.astimezone(MOSCOW).date() - timedelta(days=45)).isoformat()
                + ".json"
            ):
                old.unlink()
        return "sent"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--setup", action="store_true")
    mode.add_argument("--scheduled", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("races-2027.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("data/race-monitor"))
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        if args.check:
            observations, errors = scan(config)
            print(
                json.dumps(
                    {
                        "observations": [asdict(o) for o in observations],
                        "errors": errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(2 if errors else 0)
        result = run(config, args.state_dir, now=datetime.now(UTC), setup=args.setup)
        # Do not append one log line every minute when the gate has no work.
        if result not in {"not_due", "deferred", "already_running"} or args.setup:
            print(f"{datetime.now(UTC).isoformat()} race-monitor status={result}")
        if result in {"delivery_failed", "delivery_unknown"}:
            raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 -- never log credentials or upstream payloads
        print(f"race-monitor error={type(exc).__name__}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
