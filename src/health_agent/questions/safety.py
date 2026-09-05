"""Local emergency-language guard; it runs before any external responder call."""

from __future__ import annotations

import re

_INFORMATIONAL_QUESTION = re.compile(
    r"""^\s*(?:
        what\s+(?:causes|are|is)|why\s+(?:do|does)|
        (?:is|are)\s+.+?\s+(?:a\s+)?(?:symptom|sign)|
        что\s+(?:вызывает|такое)|какие\s+(?:причины|симптомы|признаки)
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

_DIRECT_SUBJECT = re.compile(
    r"\b(?:i|i'm|i am|my|he|she|we|they|у меня|мне|я|он|она|ему|ей)\b", re.IGNORECASE
)

# Direct statements take priority even when a question comes first. Russian
# commonly leaves out the pronoun in first-person symptom statements.
_DIRECT_EMERGENCY = re.compile(
    r"(?:\bcan(?:not|'t|’t) breathe\b|\b(?:kill|harm) myself\b|"
    r"\bwant to die\b|не могу дышать|не может дышать|задыхаюсь|"
    r"не хватает воздуха|хочу умереть|покончить с собой|убить себя|"
    r"навредить себе|перекосило лицо)",
    re.IGNORECASE,
)

_URGENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:chest pain|chest (?:pressure|tightness)|(?:pressure|tightness) in (?:my )?chest)\b",
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
    "Возможно, вам нужна экстренная помощь. Немедленно позвоните по местному номеру "
    "экстренной помощи или обратитесь в ближайшее отделение неотложной помощи — не "
    "ждите ответа онлайн. Если это безопасно, попросите кого-то быть рядом с вами."
)


def has_urgent_red_flag(question: str) -> bool:
    """Detect direct, high-confidence emergency wording without broad topic alarms."""

    if _DIRECT_EMERGENCY.search(question):
        return True
    if _INFORMATIONAL_QUESTION.search(question) and not _DIRECT_SUBJECT.search(question):
        return False
    return any(pattern.search(question) is not None for pattern in _URGENT_PATTERNS)


def urgent_response(question: str) -> str | None:
    """Return emergency guidance locally, without retrieval or a model call."""

    return URGENT_RESPONSE if has_urgent_red_flag(question) else None


def guard_urgent_question(question: str) -> str | None:
    """Explicit alias for the application-service safety boundary."""

    return urgent_response(question)
