"""Small explicit goal actions shared by all three conversations."""

from datetime import datetime
from uuid import UUID

from health_agent.pilot.contracts import Record, Store

_LEVELS = {"жизнь": "life", "год": "year", "этап": "stage", "неделя": "week"}
_ORDER = {"life": 0, "year": 1, "stage": 2, "week": 3}
_LABELS = {value: key for key, value in _LEVELS.items()}


def goal_action(
    store: Store,
    profile_id: UUID,
    domain: str,
    text: str,
    *,
    source_key: str,
    now: datetime,
) -> str | None:
    normalized = text.strip().casefold().rstrip("?!.")
    records = store.list(profile_id, "shared", "goal", limit=200)
    if normalized in {"/цели", "/goals", "мои цели", "какие у меня цели"}:
        if not records:
            return "Целей пока нет. Добавь: /цель этап | чего хочешь достичь"
        return (
            "Твои цели:\n"
            + "\n".join(
                _line(record)
                for record in sorted(
                    records,
                    key=lambda r: (_ORDER.get(str(r.payload.get("level")), 9), r.at),
                )
            )
            + "\n\nИзменить: /изменить код | новая формулировка\nСледующий шаг: /шаг код | действие\nПауза: /пауза код"
        )
    head, _, body = text.strip().partition(" ")
    command = head.casefold()
    if command not in {
        "/цель",
        "/изменить",
        "/шаг",
        "/пауза",
        "/продолжить",
        "/достигнуто",
    }:
        return None
    fields = [part.strip() for part in body.split("|")]
    try:
        if command == "/цель":
            if len(fields) not in {2, 3} or fields[0] not in _LEVELS or not fields[1]:
                raise ValueError
            level = _LEVELS[fields[0]]
            parent = _find(records, fields[2]) if len(fields) == 3 else None
            if (
                parent
                and _ORDER.get(str(parent.payload.get("level")), 9) >= _ORDER[level]
            ):
                raise ValueError
            record = store.put(
                profile_id,
                "shared",
                "goal",
                source_key,
                {
                    "title": fields[1][:1000],
                    "level": level,
                    "domain": domain,
                    "parent_id": parent.id if parent else None,
                    "status": "active",
                },
                at=now,
            )
            return "Цель сохранена.\n" + _line(record)
        goal = _find(records, fields[0])
        payload = dict(goal.payload)
        if payload.get("last_action") == source_key:
            return "Цель обновлена.\n" + _line(goal)
        if command in {"/изменить", "/шаг"}:
            if len(fields) != 2 or not fields[1]:
                raise ValueError
            payload["title" if command == "/изменить" else "next_step"] = fields[1][
                :1000
            ]
        else:
            if len(fields) != 1:
                raise ValueError
            payload["status"] = {
                "/пауза": "paused",
                "/продолжить": "active",
                "/достигнуто": "done",
            }[command]
        payload["last_action"] = source_key
        changed = store.patch(profile_id, goal.id, payload)
        return "Цель обновлена.\n" + _line(changed)
    except (ValueError, KeyError):
        return "Не получилось выбрать цель. Посмотри /цели. Добавить: /цель год | формулировка | код родительской цели (необязательно)."


def _find(records: list[Record], identifier: str) -> Record:
    matches = [r for r in records if identifier and r.id.startswith(identifier)]
    if len(matches) != 1:
        raise ValueError("goal_not_found")
    return matches[0]


def _line(record: Record) -> str:
    payload = record.payload
    status = {"active": "", "paused": " · пауза", "done": " · достигнута"}.get(
        str(payload.get("status")), ""
    )
    parent = f" → {str(payload['parent_id'])[:8]}" if payload.get("parent_id") else ""
    step = (
        f"\n  Следующий шаг: {payload['next_step']}" if payload.get("next_step") else ""
    )
    return f"{record.id[:8]} · {_LABELS.get(str(payload.get('level')), 'цель')}{parent}: {payload.get('title', '')}{status}{step}"
