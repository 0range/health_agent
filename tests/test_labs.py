from decimal import Decimal

import pytest

from health_agent.labs import (
    CandidateStatus,
    normalize_lab_result,
    parse_lab_candidates,
)
from health_agent.pdf import ExtractedPage


def test_parser_preserves_source_and_marks_candidates_for_review() -> None:
    pages = (ExtractedPage(1, "Ферритин 42 нг/мл 30-400"),)

    candidate = parse_lab_candidates(pages)[0]

    assert candidate.source_name == "Ферритин"
    assert candidate.raw_source_value == "42"
    assert candidate.parsed_value == Decimal(42)
    assert candidate.unit == "нг/мл"
    assert candidate.reference_text == "30-400"
    assert candidate.evidence_excerpt == "Ферритин 42 нг/мл 30-400"
    assert candidate.page_number == 1
    assert candidate.status == CandidateStatus.NEEDS_REVIEW
    assert candidate.status == "needs_review"


def test_parser_accepts_known_aliases_and_decimal_commas() -> None:
    pages = (
        ExtractedPage(
            2,
            "Vitamin D 25.5 ng/mL 30 - 100\nХолестерин ЛПНП 3,2 ммоль/л 0.0-3.0",
        ),
    )

    candidates = parse_lab_candidates(pages)

    assert [
        (candidate.source_name, candidate.raw_source_value, candidate.parsed_value)
        for candidate in candidates
    ] == [
        ("Vitamin D", "25.5", Decimal("25.5")),
        ("Холестерин ЛПНП", "3,2", Decimal("3.2")),
    ]


def test_parser_rejects_shifted_unit_and_preserves_printed_reference_text() -> None:
    pages = (
        ExtractedPage(
            1,
            "Глюкоза 5.1 ммоль/л 3.9-5.5\n"
            "Ферритин 42 30-400\n"
            "Ферритин 42 нг/мл <400\n"
            "Ферритин 42 нг/мл 30-400 / 20-300",
        ),
    )

    candidates = parse_lab_candidates(pages)

    assert [
        (candidate.source_name, candidate.reference_text) for candidate in candidates
    ] == [
        ("Глюкоза", "3.9-5.5"),
        ("Ферритин", "<400"),
        ("Ферритин", "30-400 / 20-300"),
    ]


def test_parser_rejects_ordinary_prose_as_a_unit() -> None:
    pages = (ExtractedPage(1, "Ferritin 42 words from a note"),)

    assert parse_lab_candidates(pages) == ()


def test_import_parser_delegates_to_shared_layouts_and_preserves_qualified_value():
    pages = (
        ExtractedPage(
            3,
            "Test: Glucose\nResult: <5.1\nUnits: mmol/L\nReference range: 3.9-5.5",
        ),
    )

    candidate = parse_lab_candidates(pages)[0]

    assert candidate.source_name == "Glucose"
    assert candidate.raw_source_value == "<5.1"
    assert candidate.parsed_value is None
    assert candidate.unit == "mmol/L"
    assert candidate.reference_text == "3.9-5.5"
    assert candidate.page_number == 3


def test_import_parser_accepts_registry_cbc_and_rejects_numeric_narrative():
    pages = (
        ExtractedPage(
            1,
            "Hemoglobin 145 g/L 130-170\nOrder code 12345 status-text",
        ),
    )

    candidates = parse_lab_candidates(pages)

    assert [candidate.source_name for candidate in candidates] == ["Hemoglobin"]


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1e999999",
        "1e-999999",
        "1000000000001",
        "0.0000000000001",
        "0" * 1000,
        "1_000",
    ],
)
def test_normalization_rejects_non_finite_or_unbounded_values(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid laboratory numeric value"):
        normalize_lab_result("ferritin", value, "ng/mL")


@pytest.mark.parametrize("value", ["0", "-1.5", "43,5", "1e3", "1e12", "1e-12"])
def test_normalization_preserves_finite_bounded_values(value: str) -> None:
    parsed, unit = normalize_lab_result("ferritin", value, "ng/mL")
    assert parsed == Decimal(value.replace(",", "."))
    assert unit == "ng/mL"
