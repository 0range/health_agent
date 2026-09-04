"""Conservative parsing of laboratory-result candidates from extracted PDF text."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from health_agent.pdf import ExtractedPage


class CandidateStatus(str, Enum):
    """Transport status for a parsed candidate before human review."""

    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class LabCandidate:
    """A source-preserving laboratory result which requires explicit review."""

    source_name: str
    source_value: Decimal
    unit: str
    reference_text: str | None
    evidence_excerpt: str
    page_number: int
    status: CandidateStatus = CandidateStatus.NEEDS_REVIEW


_ALIASES = frozenset(
    {
        "ферритин",
        "ferritin",
        "b12",
        "витамин b12",
        "vitamin b12",
        "кобаламин",
        "фолат",
        "фолиевая кислота",
        "витамин b9",
        "folate",
        "folic acid",
        "vitamin b9",
        "b9",
        "холестерин общий",
        "общий холестерин",
        "total cholesterol",
        "cholesterol",
        "холестерин лпнп",
        "лпнп",
        "ldl cholesterol",
        "ldl",
        "холестерин лпвп",
        "лпвп",
        "hdl cholesterol",
        "hdl",
        "триглицериды",
        "triglycerides",
        "железо",
        "iron",
        "витамин d",
        "vitamin d",
        "пролактин",
        "prolactin",
    }
)

_ROW_PATTERN = re.compile(
    r"^\s*(?P<name>.+?)\s*[:=]?\s+"
    r"(?P<value>[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+))\s+"
    r"(?P<unit>\S+)"
    r"(?:\s+(?P<reference>.*?))?\s*$"
)
_REFERENCE_PATTERN = re.compile(
    r"^\s*[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)\s*[-–—]\s*"
    r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)\s*$"
)
_LAB_UNITS = frozenset(
    {
        "%",
        "ng/ml",
        "ng/l",
        "нг/мл",
        "нг/л",
        "pg/ml",
        "пг/мл",
        "pmol/l",
        "пмоль/л",
        "nmol/l",
        "нмоль/л",
        "mmol/l",
        "ммоль/л",
        "umol/l",
        "µmol/l",
        "мкмоль/л",
        "ug/l",
        "µg/l",
        "мкг/л",
        "ug/dl",
        "µg/dl",
        "мкг/дл",
        "mg/dl",
        "мг/дл",
        "miu/l",
        "мме/л",
        "iu/l",
        "ме/л",
        "uiu/ml",
        "µiu/ml",
        "мкме/мл",
    }
)


def parse_lab_candidates(
    pages: tuple[ExtractedPage, ...],
) -> tuple[LabCandidate, ...]:
    """Return known, complete same-line results as candidates for human review.

    The parser does not infer values from neighbouring lines or partial ranges.
    """
    candidates: list[LabCandidate] = []
    for page in pages:
        for source_line in page.text.splitlines():
            candidate = _parse_line(page.page_number, source_line)
            if candidate is not None:
                candidates.append(candidate)
    return tuple(candidates)


def _parse_line(page_number: int, source_line: str) -> LabCandidate | None:
    match = _ROW_PATTERN.match(source_line)
    if match is None:
        return None

    source_name = match["name"].strip()
    if _normalise_name(source_name) not in _ALIASES:
        return None

    unit = match["unit"]
    if not _is_unit(unit):
        return None

    try:
        source_value = Decimal(match["value"].replace(",", "."))
    except InvalidOperation:
        return None

    raw_reference = match["reference"]
    reference_text = (
        raw_reference.strip()
        if raw_reference is not None and _REFERENCE_PATTERN.fullmatch(raw_reference)
        else None
    )
    return LabCandidate(
        source_name=source_name,
        source_value=source_value,
        unit=unit,
        reference_text=reference_text,
        evidence_excerpt=source_line,
        page_number=page_number,
    )


def _normalise_name(source_name: str) -> str:
    return " ".join(source_name.casefold().split())


def _is_unit(value: str) -> bool:
    """Accept only established units for the Slice 1 laboratory aliases."""
    normalised = value.casefold().replace("μ", "µ")
    return normalised in _LAB_UNITS
