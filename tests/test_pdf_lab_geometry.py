from __future__ import annotations

import hashlib

import pymupdf
import pytest

from health_agent.lab_extraction.registry import canonical_name, normalize_registered
from health_agent.pdf_lab_geometry import extract_lab_geometry


def gridded_pdf(*, headers=None, rows=None, column_major=True, merged_at=None):
    headers = headers or ["Test", "Result", "Reference range", "Unit", "Comment"]
    rows = rows or [["Glucose", "5.10", "3.9-5.5", "mmol/L", ""]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=300)
    xs = [30, 190, 280, 390, 480, 570]
    ys = [30 + 32 * i for i in range(len(rows) + 2)]
    for column, x in enumerate(xs):
        if merged_at == column:
            page.draw_line((x, ys[0]), (x, ys[1]))
        else:
            page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    values = [headers, *rows]
    positions = (
        [(r, c) for c in range(5) for r in range(len(values))]
        if column_major
        else [(r, c) for r in range(len(values)) for c in range(5)]
    )
    for row, column in positions:
        if values[row][column]:
            page.insert_text(
                (xs[column] + 3, ys[row] + 19), values[row][column], fontsize=7
            )
    result = pdf.tobytes()
    pdf.close()
    return result


def word_pdf(*, headers=None, rows=None, narrative=None, row_gap=35):
    headers = headers or ["Test name", "Result", "Unit", "Reference range"]
    rows = rows or [["Glucose", "5.10", "mmol/L", "3.9-5.5"]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=300)
    xs = [35, 260, 350, 440]
    for column, value in enumerate(headers):
        page.insert_text((xs[column], 45), value, fontsize=8)
    for index, row in enumerate(rows):
        y = 80 + index * row_gap
        for column, value in enumerate(row):
            page.insert_text((xs[column], y), value, fontsize=8)
    if narrative:
        page.insert_text((35, 220), narrative, fontsize=8)
    result = pdf.tobytes()
    pdf.close()
    return result


def test_gridded_column_major_source_maps_by_exact_headers():
    source = gridded_pdf(
        rows=[
            ["Glucose", "5.10", "3.9-5.5", "mmol/L", "note 999"],
            ["Hemoglobin", "145", "130-170", "g/L", ""],
        ]
    )
    unchanged = bytes(source)

    result = extract_lab_geometry(source, 1)

    assert source == unchanged
    assert result.method == "pdf_table_v1"
    assert result.source_sha256 == hashlib.sha256(source).hexdigest()
    assert len(result.rows) == 2
    assert result.rows[0].name.text == "Glucose"
    assert result.rows[0].result.text == "5.10"
    assert result.rows[0].unit.text == "mmol/L"
    assert result.rows[0].reference.text == "3.9-5.5"
    assert result.rows[0].comment is not None
    assert "999" not in result.rows[0].result.text
    assert result.rows[0].derived_line in result.text
    assert extract_lab_geometry(source, 1) == result


@pytest.mark.parametrize(
    "headers",
    [
        ["Test", "Result", "Result", "Unit", "Comment"],
        ["Test", "Result", "Reference range", "Other", "Comment"],
    ],
)
def test_gridded_rejects_duplicate_or_missing_required_header(headers):
    assert extract_lab_geometry(gridded_pdf(headers=headers), 1).rows == ()


@pytest.mark.parametrize("merged_at", [1, 2])
def test_gridded_rejects_merged_or_spanning_required_cells(merged_at):
    source = gridded_pdf(merged_at=merged_at)
    assert extract_lab_geometry(source, 1).rows == ()


@pytest.mark.parametrize(
    "row",
    [
        ["Unknown marker", "5.1", "3.9-5.5", "mmol/L", ""],
        ["Glucose", "5.1 5.2", "3.9-5.5", "mmol/L", ""],
        ["Glucose", "5.1", "3.9-5.5", "g/L", ""],
        ["Glucose", "+5.1", "3.9-5.5", "mmol/L", ""],
        ["Glucose", "5.1", "", "mmol/L", ""],
    ],
)
def test_gridded_rejects_unknown_ambiguous_flagged_or_incomplete_rows(row):
    assert extract_lab_geometry(gridded_pdf(rows=[row]), 1).rows == ()


def test_kdl_header_anchors_two_adjacent_rows_and_wrapped_name():
    source = word_pdf(
        rows=[
            ["C-reactive", "", "", ""],
            ["protein", "4", "mg/L", "0-5"],
            ["Glucose", "5.2", "mmol/L", "3.9-5.5"],
        ],
        row_gap=16,
    )

    result = extract_lab_geometry(source, 1)

    assert [row.name.text for row in result.rows] == ["C-reactive protein", "Glucose"]


def test_kdl_rejects_multiple_result_words_and_does_not_consume_neighbor():
    source = word_pdf(
        rows=[
            ["Glucose", "5.1 5.2", "mmol/L", "3.9-5.5"],
            ["Hemoglobin", "145", "g/L", "130-170"],
        ]
    )
    result = extract_lab_geometry(source, 1)
    assert [row.name.text for row in result.rows] == ["Hemoglobin"]


@pytest.mark.parametrize(
    ("source_name", "source_unit", "expected"),
    [
        ("Средний объем эритроцитов (MCV)", "fL", "mcv"),
        ("Средняя концентрация Hb в эритроцитах (МСНС)", "g/L", "mchc"),
        ("Среднее содержание гемоглобина в эритроците (МСН)", "пг/кл", "mch"),
        ("Средний объем тромбоцитов (MPV)", "fL", "mpv"),
    ],
)
def test_allowed_aliases_are_exact_and_unit_compatible(source_name, source_unit, expected):
    canonical = canonical_name(source_name)
    assert canonical == expected
    assert normalize_registered(canonical, "1", source_unit)[1] != ""


def test_aliases_do_not_use_prefixes_and_pg_per_cell_is_mch_only():
    source_name = "Средний объем эритроцитов (MCV) extra"
    assert canonical_name(source_name).startswith("unmapped_")
    with pytest.raises(ValueError, match="unsupported_lab_normalization"):
        normalize_registered("glucose", "1", "пг/кл")


def test_unheaded_numeric_narrative_and_dates_never_create_rows():
    source = word_pdf(
        headers=["Heading", "Other", "More", "Notes"],
        rows=[["Narrative", "5.1", "mmol/L", "3.9-5.5"]],
        narrative="Report date 2099-01-01 protocol 42 mmol/L",
    )
    assert extract_lab_geometry(source, 1).rows == ()


@pytest.mark.parametrize("page", [0, 2])
def test_invalid_pdf_or_page_uses_one_safe_error(page):
    source = gridded_pdf()
    with pytest.raises(ValueError, match="invalid_pdf_geometry"):
        extract_lab_geometry(source, page)
    with pytest.raises(ValueError, match="invalid_pdf_geometry"):
        extract_lab_geometry(b"not a pdf", 1)
