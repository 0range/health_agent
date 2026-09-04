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
    raw_source_value: str
    parsed_value: Decimal
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


@dataclass(frozen=True)
class UnitNormalization:
    canonical_unit: str
    factor: Decimal = Decimal(1)


class UnsupportedNormalization(ValueError):
    """Raised when a value/unit pair cannot be normalized conservatively."""


_UNIT_NORMALIZATIONS: dict[tuple[str, str], UnitNormalization] = {
    ("ferritin", "ng/ml"): UnitNormalization("ng/mL"),
    ("ferritin", "нг/мл"): UnitNormalization("ng/mL"),
    ("ferritin", "ug/l"): UnitNormalization("ng/mL"),
    ("ferritin", "µg/l"): UnitNormalization("ng/mL"),
    ("ferritin", "мкг/л"): UnitNormalization("ng/mL"),
    ("vitamin_b12", "pg/ml"): UnitNormalization("pg/mL"),
    ("vitamin_b12", "пг/мл"): UnitNormalization("pg/mL"),
    ("folate", "ng/ml"): UnitNormalization("ng/mL"),
    ("folate", "нг/мл"): UnitNormalization("ng/mL"),
    ("total_cholesterol", "mmol/l"): UnitNormalization("mmol/L"),
    ("total_cholesterol", "ммоль/л"): UnitNormalization("mmol/L"),
    ("ldl_cholesterol", "mmol/l"): UnitNormalization("mmol/L"),
    ("ldl_cholesterol", "ммоль/л"): UnitNormalization("mmol/L"),
    ("hdl_cholesterol", "mmol/l"): UnitNormalization("mmol/L"),
    ("hdl_cholesterol", "ммоль/л"): UnitNormalization("mmol/L"),
    ("triglycerides", "mmol/l"): UnitNormalization("mmol/L"),
    ("triglycerides", "ммоль/л"): UnitNormalization("mmol/L"),
    ("iron", "umol/l"): UnitNormalization("µmol/L"),
    ("iron", "µmol/l"): UnitNormalization("µmol/L"),
    ("iron", "мкмоль/л"): UnitNormalization("µmol/L"),
    ("vitamin_d", "ng/ml"): UnitNormalization("ng/mL"),
    ("vitamin_d", "нг/мл"): UnitNormalization("ng/mL"),
    ("prolactin", "ng/ml"): UnitNormalization("ng/mL"),
    ("prolactin", "нг/мл"): UnitNormalization("ng/mL"),
}


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


def looks_like_lab_document(pages: tuple[ExtractedPage, ...]) -> bool:
    """Recognize numeric lab-like rows without treating ordinary prose as a lab."""
    for page in pages:
        for source_line in page.text.splitlines():
            match = _ROW_PATTERN.match(source_line)
            if match is not None and _is_unit(match["unit"]):
                return True
            normalized_line = _normalise_name(source_line)
            if any(alias in normalized_line for alias in _ALIASES) and any(
                character.isdigit() for character in source_line
            ):
                return True
    return False


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
        raw_source_value = match["value"]
        parsed_value = parse_decimal_token(raw_source_value)
    except ValueError:
        return None

    raw_reference = match["reference"]
    reference_text = (
        raw_reference.strip()
        if raw_reference is not None and _REFERENCE_PATTERN.fullmatch(raw_reference)
        else None
    )
    return LabCandidate(
        source_name=source_name,
        raw_source_value=raw_source_value,
        parsed_value=parsed_value,
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


def parse_decimal_token(raw_value: str) -> Decimal:
    """Parse a source token without changing the token retained as evidence."""
    try:
        return Decimal(raw_value.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("Invalid laboratory numeric value") from error


def normalize_lab_result(
    canonical_name: str, raw_value: str, source_unit: str | None
) -> tuple[Decimal, str]:
    """Normalize only explicitly supported analyte/unit pairs."""
    if source_unit is None:
        raise UnsupportedNormalization("Unsupported normalization: missing source unit")
    unit_key = source_unit.strip().casefold().replace("μ", "µ")
    normalization = _UNIT_NORMALIZATIONS.get((canonical_name, unit_key))
    if normalization is None:
        raise UnsupportedNormalization(
            f"Unsupported normalization for {canonical_name!r} and source unit"
        )
    return (
        parse_decimal_token(raw_value) * normalization.factor,
        normalization.canonical_unit,
    )
