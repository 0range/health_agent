"""Local emergency-language guard; it runs before any external responder call."""

from __future__ import annotations

import re

_INFORMATIONAL_QUESTION = re.compile(
    r"""^\s*(?:
        what|why|how|when|where|can|could|is|are|do|does|
        что|почему|как|когда|где|какие
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

_DIRECT_SUBJECT = re.compile(
    r"\b(?:i|i'm|i am|my|у меня|мне|я)\b", re.IGNORECASE
)

_URGENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:chest pain|chest (?:pressure|tightness)|pressure|tightness in (?:my )?chest)\b",
        r"\b(?:can(?:not|'t) breathe|trouble breathing|difficulty breathing|shortness of breath)\b",
        r"\b(?:suicid(?:al|e)|kill myself|harm myself|want to die|feel like killing myself)\b",
        r"\b(?:stroke symptoms?|face droop|one[- ]sided weakness)\b",
        r"\b(?:severe|uncontrolled) bleeding\b",
        r"(?:болит грудь|(?:бол(?:ит|ь)|дав(?:ит|ление)|тяжесть) в груд(?:и|ь))\b",
        r"(?:не могу дышать|не хватает воздуха|трудно дышать|задыхаюсь|одышк[аи])\b",
        r"(?:суицид|покончить с собой|убить себя|навредить себе|хочу умереть)\b",
        r"(?:признаки инсульта|перекосило лицо|слабость с одной стороны)\b",
        r"\b(?:сильное|неконтролируемое) кровотечение\b",
    )
)

URGENT_RESPONSE = (
    "This may need emergency care. Call your local emergency number now or go to "
    "the nearest emergency department; do not wait for an online answer. "
    "Если это безопасно, попросите кого-то быть рядом с вами."
)


def has_urgent_red_flag(question: str) -> bool:
    """Detect direct, high-confidence emergency wording without broad topic alarms."""

    if _INFORMATIONAL_QUESTION.search(question) and not _DIRECT_SUBJECT.search(question):
        return False
    return any(pattern.search(question) is not None for pattern in _URGENT_PATTERNS)


def urgent_response(question: str) -> str | None:
    """Return emergency guidance locally, without retrieval or a model call."""

    return URGENT_RESPONSE if has_urgent_red_flag(question) else None


def guard_urgent_question(question: str) -> str | None:
    """Explicit alias for the application-service safety boundary."""

    return urgent_response(question)
