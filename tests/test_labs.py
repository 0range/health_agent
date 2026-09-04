from decimal import Decimal

from health_agent.labs import CandidateStatus, parse_lab_candidates
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
            "Vitamin D 25.5 ng/mL 30 - 100\n"
            "Холестерин ЛПНП 3,2 ммоль/л 0.0-3.0",
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


def test_parser_rejects_unknown_names_and_does_not_guess_incomplete_ranges() -> None:
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

    assert [(candidate.source_name, candidate.reference_text) for candidate in candidates] == [
        ("Ферритин", None),
        ("Ферритин", None),
    ]


def test_parser_rejects_ordinary_prose_as_a_unit() -> None:
    pages = (ExtractedPage(1, "Ferritin 42 words from a note"),)

    assert parse_lab_candidates(pages) == ()
