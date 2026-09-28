"""The selected personal bridge-snack exception, independent of model output."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

CATEGORY = "bridge_snack"
LABEL = "Небольшой перекус"
FOLLOWUP = timedelta(minutes=60)
WINDOW_END = timedelta(minutes=90)
DELIVERY_END = timedelta(minutes=150)
_NEXT = {"breakfast": "lunch", "lunch": "afternoon", "afternoon": "dinner"}
_NUTS = re.compile(
    r"(?:(?:я\s+)?(?:съел[аи]?|поел[аи]?|перекусил[аи]?|/ел|/?перекус)\s*:?\s*)?"
    r"(?P<count>\d+)\s*(?P<half>половин\w*\s+)?(?:штук[иа]?\s+|шт\.?\s*)?"
    r"(?P<food>орех\w*(?:\s+(?:миндал\w*|фундук\w*))?|миндал\w*(?:\s+орех\w*)?|"
    r"фундук\w*|грецк\w*\s+орех\w*)[.!\s]*", re.IGNORECASE,
)


def enabled(protocol: dict[str, Any]) -> bool:
    rule = protocol.get("bridge_snack")
    return isinstance(rule, dict) and rule.get("enabled") is True


def recognizes(text: str, protocol: dict[str, Any]) -> bool:
    """Conservative standalone intake; never substring-match nuts inside a meal."""
    if not enabled(protocol):
        return False
    cleaned = re.sub(r"\b(?:в\s+)?(?:[01]?\d|2[0-3]):[0-5]\d\b", "", text.casefold())
    cleaned = re.sub(r"\bдесять\b", "10", cleaned).strip()
    match = _NUTS.fullmatch(cleaned)
    if match is None:
        return False
    count = int(match["count"])
    walnut = "грецк" in match["food"]
    if match["half"] and not walnut:
        return False
    return 1 <= count <= (5 if walnut and not match["half"] else 10)


def next_category(payload: dict[str, Any]) -> str | None:
    value = (payload.get("next_meal_category") if payload.get("category") == CATEGORY
             else _NEXT.get(str(payload.get("category"))))
    return value if value in {"breakfast", "lunch", "afternoon", "dinner"} else None


def anchor(payload: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(payload["occurred_at"]))


def selected_rule(protocol: dict[str, Any]) -> dict[str, Any]:
    return {"enabled": enabled(protocol), "followup_minutes": [60, 90],
            "counts_as_full_meal": False, "calories_counted": True,
            "options": ["до 10 миндалин или фундука", "до 10 половинок грецкого ореха"],
            "source": "user_selected", "not_universal_medical_rule": True}


def help_text() -> str:
    return (
        "🥜 Если полноценная еда откладывается: по твоему плану можно до 10 миндалин "
        "или фундука, либо до 10 половинок грецкого ореха. "
        "После — полноценная еда через 1–1,5 часа. "
        "Калории учитываю, полный приём этим не закрываю. "
        "Когда съешь, напиши, например: «съел 10 орехов миндаля»."
    )
