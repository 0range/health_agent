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
    unknown = parse_local("Synthetic marker 2.5 mg/L")
    assert unknown.candidates == () and unknown.unresolved
    unknown_unit = parse_local("Ferritin 42 mystery-unit")
    assert unknown_unit.candidates == () and unknown_unit.unresolved


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


def test_unknown_name_and_unit_are_unresolved_not_published():
    result = parse_local("Synthetic marker 2.5 mystery-unit")
    assert result.candidates == ()
    assert result.unresolved


def test_numeric_protocol_prose_and_name_prefix_pollution_are_not_candidates():
    for text in (
        "Order code 12345 status-text",
        "Glucose commentary 5.1 mmol/L",
        "Previous Glucose 5.1 mmol/L",
    ):
        result = parse_local(text)
        assert result.candidates == ()
        assert result.unresolved


def test_labelled_and_swapped_pipe_layouts_preserve_exact_source():
    labelled = (
        "Показатель: Глюкоза\n"
        "Результат: 5,1\n"
        "Единицы: ммоль/л\n"
        "Референс: 3,9-5,5"
    )
    row = parse_local(labelled).candidates[0]
    assert (row.source_name, row.source_value, row.source_unit) == (
        "Глюкоза",
        "5,1",
        "ммоль/л",
    )
    assert row.reference_text == "3,9-5,5"
    assert row.evidence_excerpt == labelled

    swapped = "Глюкоза | ммоль/л | <5,1 | 3,9-5,5"
    row = parse_local(swapped).candidates[0]
    assert row.source_value == "<5,1"
    assert row.parsed_value is None
    assert row.evidence_excerpt == swapped

    same_line = "Analyte: Glucose Result: 5.1 Units: mmol/L Reference: 3.9-5.5"
    row = parse_local(same_line).candidates[0]
    assert row.evidence_excerpt == same_line


@pytest.mark.parametrize(
    "text",
    [
        "Показатель: Глюкоза\nРезультат: 5,1",
        "Показатель: Глюкоза\nРезультат: 5,1\nРезультат: 5,2\nЕдиницы: ммоль/л",
        "Показатель: Глюкоза\nРезультат: 5,1\nЕдиницы: mystery",
        "Глюкоза | ммоль/л | 5,1 | 3,9-5,5 | extra",
        "Глюкоза | 5,1 | 5,2 | ммоль/л",
    ],
)
def test_incomplete_ambiguous_or_extra_layouts_stay_unresolved(text):
    result = parse_local(text)
    assert result.candidates == ()
    assert result.unresolved


def test_cloud_layout_proof_rejects_reassigned_or_omitted_fields():
    text = "Glucose | mmol/L | 5.1 | 3.9-5.5"
    accepted = payload(
        source_name="Glucose",
        source_value="5.1",
        source_unit="mmol/L",
        reference_text="3.9-5.5",
        evidence_excerpt=text,
    )
    assert validate_candidates(accepted, text)[0].canonical_name == "glucose"
    with pytest.raises(ValueError, match="candidate_evidence_mismatch"):
        validate_candidates(
            payload(
                source_name="Glucose",
                source_value="5.1",
                source_unit="mmol/L",
                reference_text=None,
                evidence_excerpt=text,
            ),
            text,
        )


def test_cloud_validation_rejects_multirow_and_malformed_explicit_excerpts():
    for text in (
        "Glucose 5.1 mmol/L\nALT 20 U/L",
        "Glucose | 5.1 | mmol/L | 3.9-5.5 | extra",
    ):
        with pytest.raises(ValueError, match="candidate_evidence_mismatch"):
            validate_candidates(
                payload(
                    source_name="Glucose",
                    source_value="5.1",
                    source_unit="mmol/L",
                    reference_text=None,
                    evidence_excerpt=text,
                ),
                text,
            )


@pytest.mark.parametrize(
    ("page", "excerpt", "reference"),
    [
        (
            "NotGlucose | mmol/L | 5.1",
            "Glucose | mmol/L | 5.1",
            None,
        ),
        (
            "Glucose | mmol/L | 5.10",
            "Glucose | mmol/L | 5.1",
            None,
        ),
        (
            "Glucose | mmol/L | 5.1 | 3.9-5.5",
            "Glucose | mmol/L | 5.1",
            None,
        ),
        (
            "Test: NotGlucose\nResult: 5.1\nUnits: mmol/L",
            "Test: Glucose\nResult: 5.1\nUnits: mmol/L",
            None,
        ),
        (
            "Test: Glucose\nResult: 5.10\nUnits: mmol/L",
            "Test: Glucose\nResult: 5.1\nUnits: mmol/L",
            None,
        ),
        (
            "Test: Glucose\nResult: 5.1\nUnits: mmol/L\nReference: 3.9-5.5",
            "Test: Glucose\nResult: 5.1\nUnits: mmol/L",
            None,
        ),
    ],
)
def test_explicit_layout_requires_complete_page_record_boundaries(
    page, excerpt, reference
):
    with pytest.raises(ValueError, match="candidate_evidence_mismatch"):
        validate_candidates(
            payload(
                source_name="Glucose",
                source_value="5.1",
                source_unit="mmol/L",
                reference_text=reference,
                evidence_excerpt=excerpt,
            ),
            page,
        )


def test_pipe_layout_rejects_second_row_absorbed_into_reference():
    text = "Glucose | mmol/L | 5.1 | 3.9-5.5\nALT 20 U/L"
    with pytest.raises(ValueError, match="candidate_evidence_mismatch"):
        validate_candidates(
            payload(
                source_name="Glucose",
                source_value="5.1",
                source_unit="mmol/L",
                reference_text="3.9-5.5\nALT 20 U/L",
                evidence_excerpt=text,
            ),
            text,
        )


def test_explicit_records_preserve_trailing_flags_and_labelled_optional_crop():
    for text in (
        "Glucose | mmol/L | 5.1 | 3.9-5.5 H",
        "ALT | 53 | U/L | 0-41 H",
    ):
        row = parse_local(text).candidates[0]
        assert row.source_flag == "H"
        assert row.reference_text in {"3.9-5.5", "0-41"}
        cloud = validate_candidates(
            {
                "candidates": [
                    {
                        "source_name": row.source_name,
                        "source_value": row.source_value,
                        "source_unit": row.source_unit,
                        "reference_text": row.reference_text,
                        "source_flag": row.source_flag,
                        "evidence_excerpt": text,
                    }
                ]
            },
            text,
        )[0]
        assert cloud.source_flag == "H"

    page = "Patient: Synthetic\nTest: Glucose\nResult: 5.1\nUnits: mmol/L\nComment: Synthetic"
    excerpt = "Test: Glucose\nResult: 5.1\nUnits: mmol/L"
    accepted = validate_candidates(
        payload(
            source_name="Glucose",
            source_value="5.1",
            source_unit="mmol/L",
            reference_text=None,
            evidence_excerpt=excerpt,
        ),
        page,
    )[0]
    assert accepted.reference_text is None


def test_flag_from_unrelated_later_row_is_rejected():
    text = "ALT 53 U/L 0-41\nOther marker H"
    with pytest.raises(ValueError, match="candidate_evidence_mismatch"):
        validate_candidates(
            {
                "candidates": [
                    {
                        "source_name": "ALT",
                        "source_value": "53",
                        "source_unit": "U/L",
                        "reference_text": "0-41",
                        "source_flag": "H",
                        "evidence_excerpt": text,
                    }
                ]
            },
            text,
        )
