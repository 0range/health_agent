"""Local emergency-language guard; it runs before any external responder call."""

from __future__ import annotations

import re

_URGENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:chest pain|pressure in (?:my )?chest)\b",
        r"\b(?:can(?:not|'t) breathe|difficulty breathing|shortness of breath)\b",
        r"\b(?:suicid(?:al|e)|kill myself|harm myself)\b",
        r"\b(?:stroke symptoms?|face droop|one[- ]sided weakness)\b",
        r"\b(?:severe|uncontrolled) bleeding\b",
        r"\b(?:боль|давление) в груди\b",
        r"\b(?:не могу дышать|трудно дышать|одышк[аи])\b",
        r"\b(?:суицид|покончить с собой|убить себя|навредить себе)\b",
        r"\b(?:признаки инсульта|перекосило лицо|слабость с одной стороны)\b",
        r"\b(?:сильное|неконтролируемое) кровотечение\b",
    )
)

URGENT_RESPONSE = (
    "This may need emergency care. Call your local emergency number now or go to "
    "the nearest emergency department; do not wait for an online answer. "
    "Если это безопасно, попросите кого-то быть рядом с вами."
)


def has_urgent_red_flag(question: str) -> bool:
    """Return whether wording directly indicates a possible medical emergency."""

    return any(pattern.search(question) is not None for pattern in _URGENT_PATTERNS)


def urgent_response(question: str) -> str | None:
    """Return emergency guidance locally, without retrieval or a model call."""

    return URGENT_RESPONSE if has_urgent_red_flag(question) else None


def guard_urgent_question(question: str) -> str | None:
    """Explicit alias for the application-service safety boundary."""

    return urgent_response(question)
