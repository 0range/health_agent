from decimal import Decimal

import pytest

from health_agent.lab_extraction.registry import canonical_name, normalize_registered
from health_agent.lab_extraction.validation import parse_local, validate_candidates


def test_messy_russian_and_english_labs_extend_old_aliases():
    text = (
        "Дата взятия: 04.09.2026\n"
        "Гемоглобин | 145 | г/л | 130–170\n"
        "Лейкоциты 5,61 10^9/л 4.0-10.0\n"
        "АЛТ 23 Ед/л <41\n"
        "Креатинин 81 мкмоль/л 62-106\n"
        "TSH 1.76 mIU/L 0.27-4.20\n"
        "Free T4 15.2 pmol/L 12-22\n"
        "C-reactive protein <0.5 mg/L 0-5"
    )
    result = parse_local(text)
    assert [row.canonical_name for row in result.candidates] == [
        "hemoglobin",
        "white_blood_cells",
        "alt",
        "creatinine",
        "tsh",
        "free_t4",
        "crp",
    ]
    assert result.candidates[1].parsed_value == Decimal("5.61")
    assert result.candidates[-1].source_value == "<0.5"
    assert result.candidates[-1].parsed_value is None
    assert all(row.evidence_excerpt in text for row in result.candidates)
    assert normalize_registered("hemoglobin", "145", "г/л") == (Decimal(145), "g/L")
    assert canonical_name("Гамма-глутамилтрансфераза") == "ggt"


def test_unknown_analyte_and_unknown_unit_are_not_silently_normalized():
    unknown = parse_local("Synthetic marker 2.5 mg/L").candidates[0]
    assert unknown.canonical_name.startswith("unmapped_")
    with pytest.raises(ValueError):
        normalize_registered(unknown.canonical_name, "2.5", "mg/L")
    row = parse_local("Ferritin 42 mystery-unit").candidates[0]
    assert row.source_unit == "mystery-unit"
    with pytest.raises(ValueError):
        normalize_registered("ferritin", "42", "mystery-unit")


def payload(**changes):
    row = {
        "source_name": "Глюкоза",
        "source_value": "5,1",
        "source_unit": "ммоль/л",
        "reference_text": "3.9-5.5",
        "source_flag": None,
        "evidence_excerpt": "Глюкоза 5,1 ммоль/л 3.9-5.5",
    }
    row.update(changes)
    return {"candidates": [row]}


@pytest.mark.parametrize(
    "change",
    [
        {"source_value": "99"},
        {"source_value": "5"},
        {"source_name": "private invented analyte"},
        {"evidence_excerpt": "fabricated evidence"},
        {"status": "verified"},
        {"source_value": "NaN"},
        {"source_value": "1e999999"},
    ],
)
def test_structured_candidates_require_exact_bounded_evidence(change):
    with pytest.raises(ValueError):
        validate_candidates(payload(**change), "Глюкоза 5,1 ммоль/л 3.9-5.5")


def test_structured_rows_cannot_add_instructions_or_exceed_count():
    text = "Глюкоза 5,1 ммоль/л 3.9-5.5"
    assert validate_candidates(payload(), text)[0].canonical_name == "glucose"
    with pytest.raises(ValueError):
        validate_candidates({"candidates": payload()["candidates"] * 41}, text)
    with pytest.raises(ValueError):
        validate_candidates({**payload(), "instructions": "publish"}, text)


def test_multiline_layout_and_unread_rows_request_fallback():
    assert parse_local("Ferritin\n42 ng/mL\n30-400").unresolved
    assert parse_local("Ferritin 42 ng/mL").unresolved is False
    assert (
        parse_local("Patient: Synthetic\nCollection date: 2026-09-04").candidates == ()
    )


def test_printed_flags_and_unambiguous_ranges_preserve_not_infer_meaning():
    row = parse_local("ALT 53 H U/L 0-41").candidates[0]
    assert row.source_flag == "H"
    assert (row.reference_low, row.reference_high) == (Decimal(0), Decimal(41))
    ambiguous = parse_local("ALT 53 U/L male 0-41 / female 0-33").candidates[0]
    assert ambiguous.reference_low is None and ambiguous.reference_high is None
    assert ambiguous.reference_text == "male 0-41 / female 0-33"


@pytest.mark.parametrize("printed", ["-5", "<5", "145"])
def test_numeric_evidence_cannot_drop_sign_qualifier_or_digits(printed):
    text = f"Glucose {printed} mmol/L"
    row = {
        "source_name": "Glucose",
        "source_value": "5",
        "source_unit": "mmol/L",
        "reference_text": None,
        "source_flag": None,
        "evidence_excerpt": text,
    }
    with pytest.raises(ValueError):
        validate_candidates({"candidates": [row]}, text)


@pytest.mark.parametrize(
    "name,unit,reference,text",
    [
        ("Glucose", "mg/L", None, "Glucose 5 mg/L/day"),
        ("Free T4", "pmol/L", None, "Free T4-index 5 pmol/L"),
        ("Glucose", "mmol/L", "3.9-5.5", "Glucose 5 mmol/L 13.9-5.50"),
    ],
)
def test_fields_must_not_be_shortened_tokens(name, unit, reference, text):
    row = {
        "source_name": name,
        "source_value": "5",
        "source_unit": unit,
        "source_flag": None,
        "reference_text": reference,
        "evidence_excerpt": text,
    }
    with pytest.raises(ValueError):
        validate_candidates({"candidates": [row]}, text)


@pytest.mark.parametrize(
    "text,flag",
    [
        ("ALT 53 H U/L 0-41", "H"),
        ("ALT 53 U/L H 0-41", "H"),
        ("ALT 53 U/L 0-41 H", "H"),
        ("ALT 53 U/L ↑", "↑"),
        ("ALT 53 U/L 0-41 *", "*"),
    ],
)
def test_common_printed_flag_positions_are_not_lost(text, flag):
    row = parse_local(text).candidates[0]
    assert row.source_flag == flag


def test_both_unknown_name_and_unit_are_retained_review_only():
    row = parse_local("Synthetic marker 2.5 mystery-unit").candidates[0]
    assert row.canonical_name.startswith("unmapped_")
    assert row.source_unit == "mystery-unit"
    with pytest.raises(ValueError):
        normalize_registered(row.canonical_name, row.source_value, row.source_unit)
