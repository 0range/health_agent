"""Period-scoped goal snapshots shared by proposals and short weekly advice."""

from datetime import date
from typing import Any
from uuid import UUID

from health_agent.pilot.contracts import Store


def current_goals(store: Store, profile: UUID, first: date, last: date) -> list[dict[str, Any]]:
    return [r.payload for r in store.list(profile, "shared", "goal", limit=200)
            if r.payload.get("status", "active") in {"active", "planned"}
            and r.payload.get("priority") == "primary"
            and r.payload.get("period_start", "0000") <= last.isoformat()
            and r.payload.get("period_end", "9999") >= first.isoformat()]


def training_targets(goals: list[dict[str, Any]]) -> dict[str, int]:
    for goal in goals:
        targets = goal.get("weekly_targets")
        if (isinstance(targets, dict) and targets and set(targets) <= {"strength", "padel"}
                and all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 7 for v in targets.values())
                and sum(targets.values()) <= 7):
            return dict(targets)
    return {}


def training_label(targets: dict[str, int]) -> str:
    parts = []
    if "strength" in targets:
        n = targets["strength"]
        parts.append(f"{n} " + ("силовая" if n == 1 else "силовые" if n < 5 else "силовых"))
    if "padel" in targets:
        n = targets["padel"]
        parts.append(f"{n} " + ("падел" if n == 1 else "падела" if n < 5 else "игр в падел"))
    return " + ".join(parts)


def goal_label(goals: list[dict[str, Any]]) -> str:
    outcome = next((g for g in goals if (g.get("target") or {}).get("metric") == "fat_mass_loss_kg"), None)
    parts = [str(outcome.get("brief_title") or outcome["title"])[:100]] if outcome else []
    targets = training_targets(goals)
    if targets:
        parts.append(training_label(targets))
    if not parts:
        parts = [str(g.get("brief_title") or g.get("title", ""))[:100] for g in goals[:2]]
    return "; ".join(parts) or "ориентир пока не выбран — /цели"
