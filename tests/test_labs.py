from decimal import Decimal

import pytest

from health_agent.labs import (
    CandidateStatus,
    UnsupportedNormalization,
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


def test_import_parser_preserves_separated_colon_and_pipe_flag():
    pages = (
        ExtractedPage(
            1,
            "Ferritin : 42 ng/mL 30-400\nALT | 53 | U/L | 0-41 H",
        ),
    )

    candidates = parse_lab_candidates(pages)

    assert [candidate.source_name for candidate in candidates] == ["Ferritin", "ALT"]
    assert candidates[1].source_flag == "H"
    assert candidates[1].reference_text == "0-41"


def test_import_parser_preserves_exact_five_field_pipe_flag_only():
    pages = (
        ExtractedPage(
            1,
            "Glucose | 5.1 | mmol/L | H | 3.9-5.5\n"
            "Glucose | mmol/L | 5.2 | H | 3.9-5.5\n"
            "Glucose | 5.3 | mmol/L | forged | 3.9-5.5",
        ),
    )

    candidates = parse_lab_candidates(pages)

    assert len(candidates) == 1
    assert candidates[0].source_flag == "H"
    assert candidates[0].reference_text == "3.9-5.5"


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


@pytest.mark.parametrize(
    ("canonical_name", "source_unit", "normalized_unit"),
    [
        ("white_blood_cells", "тыс/мкл", "10^9/L"),
        ("white_blood_cells", "*10^9/л", "10^9/L"),
        ("white_blood_cells", "10*9/литр", "10^9/L"),
        ("platelets", "10^9/литр", "10^9/L"),
        ("tsh", "мкМЕ/мл", "uIU/mL"),
        ("alt", "Ед./л", "U/L"),
        ("ast", "Ед/л", "U/L"),
    ],
)
def test_normalization_accepts_exact_bootstrap_unit_spellings_without_source_rewrite(
    canonical_name: str, source_unit: str, normalized_unit: str
) -> None:
    source_value = "12,5"

    assert normalize_lab_result(canonical_name, source_value, source_unit) == (
        Decimal("12.5"),
        normalized_unit,
    )
    assert source_value == "12,5"
    assert source_unit in {
        "тыс/мкл",
        "*10^9/л",
        "10*9/литр",
        "10^9/литр",
        "мкМЕ/мл",
        "Ед./л",
        "Ед/л",
    }


@pytest.mark.parametrize(
    ("canonical_name", "source_unit"),
    [
        ("white_blood_cells", "тыс/мл"),
        ("platelets", "10^9/дл"),
        ("alt", "Ед./мл"),
    ],
)
def test_normalization_rejects_similar_but_unsupported_bootstrap_units(
    canonical_name: str, source_unit: str
) -> None:
    with pytest.raises(UnsupportedNormalization, match="Unsupported normalization"):
        normalize_lab_result(canonical_name, "12,5", source_unit)


def test_prolactin_micro_international_units_remain_distinct_from_mass_units() -> None:
    source_value = "762.00"

    assert normalize_lab_result("prolactin", source_value, "мкМЕ/мл") == (
        Decimal("762.00"),
        "uIU/mL",
    )
    assert normalize_lab_result("prolactin", source_value, "нг/мл") == (
        Decimal("762.00"),
        "ng/mL",
    )
