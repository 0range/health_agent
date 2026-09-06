"""Conservative parsing of laboratory-result candidates from extracted PDF text."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from health_agent.lab_extraction.registry import (
    canonical_name,
    known_unit,
    normalize_registered,
)
from health_agent.lab_extraction.validation import parse_page_candidates
from health_agent.pdf import ExtractedPage

MAX_LAB_TOKEN_CHARACTERS = 64
MAX_LAB_ABSOLUTE_VALUE = Decimal("1e12")
MAX_LAB_EXPONENT = 12
MAX_LAB_SIGNIFICANT_DIGITS = 28


class CandidateStatus(str, Enum):
    """Transport status for a parsed candidate before human review."""

    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class LabCandidate:
    """A source-preserving laboratory result which requires explicit review."""

    source_name: str
    raw_source_value: str
    parsed_value: Decimal | None
    unit: str
    reference_text: str | None
    evidence_excerpt: str
    page_number: int
    status: CandidateStatus = CandidateStatus.NEEDS_REVIEW
    source_flag: str | None = None


_ROW_PATTERN = re.compile(
    r"^\s*(?P<name>.+?)\s*[:=]?\s+"
    r"(?P<value>[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+))\s+"
    r"(?P<unit>\S+)"
    r"(?:\s+(?P<reference>.*?))?\s*$"
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
        for candidate in parse_page_candidates(page.text).candidates:
            candidates.append(
                LabCandidate(
                    source_name=candidate.source_name,
                    raw_source_value=candidate.source_value,
                    parsed_value=candidate.parsed_value,
                    unit=candidate.source_unit,
                    reference_text=candidate.reference_text,
                    evidence_excerpt=candidate.evidence_excerpt,
                    page_number=page.page_number,
                    source_flag=candidate.source_flag,
                )
            )
    return tuple(candidates)


def looks_like_lab_document(pages: tuple[ExtractedPage, ...]) -> bool:
    """Recognize numeric lab-like rows without treating ordinary prose as a lab."""
    for page in pages:
        if parse_page_candidates(page.text).candidates:
            return True
        for source_line in page.text.splitlines():
            match = _ROW_PATTERN.match(source_line)
            if (
                match is not None
                and not canonical_name(match["name"].strip()).startswith("unmapped_")
                and known_unit(match["unit"])
            ):
                return True
    return False


def parse_decimal_token(raw_value: str) -> Decimal:
    """Parse a bounded finite decimal, without changing retained source evidence.

    These are technical storage/format bounds, not medical reference ranges.
    Reject exceptional values before arithmetic or any verified-state mutation.
    """
    if len(raw_value) > MAX_LAB_TOKEN_CHARACTERS:
        raise ValueError("Invalid laboratory numeric value")
    token = raw_value.strip().replace(",", ".")
    if (
        re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", token
        )
        is None
    ):
        raise ValueError("Invalid laboratory numeric value")
    try:
        value = Decimal(token)
    except InvalidOperation:
        raise ValueError("Invalid laboratory numeric value") from None
    if not value.is_finite():
        raise ValueError("Invalid laboratory numeric value")
    exponent = value.as_tuple().exponent
    if (
        not isinstance(exponent, int)
        or not -MAX_LAB_EXPONENT <= exponent <= MAX_LAB_EXPONENT
        or value.copy_abs() > MAX_LAB_ABSOLUTE_VALUE
        or len(value.as_tuple().digits) > MAX_LAB_SIGNIFICANT_DIGITS
    ):
        raise ValueError("Invalid laboratory numeric value")
    return value


def normalize_lab_result(
    canonical_name: str, raw_value: str, source_unit: str | None
) -> tuple[Decimal, str]:
    """Normalize only explicitly supported analyte/unit pairs."""
    if source_unit is None:
        raise UnsupportedNormalization("Unsupported normalization: missing source unit")
    unit_key = source_unit.strip().casefold().replace("μ", "µ")
    normalization = _UNIT_NORMALIZATIONS.get((canonical_name, unit_key))
    if normalization is None:
        try:
            return normalize_registered(canonical_name, raw_value, source_unit)
        except ValueError:
            raise UnsupportedNormalization("Unsupported normalization") from None
    return (
        parse_decimal_token(raw_value) * normalization.factor,
        normalization.canonical_unit,
    )
