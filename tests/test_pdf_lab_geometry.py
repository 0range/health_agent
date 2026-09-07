from __future__ import annotations

import hashlib

import pymupdf
import pytest

from health_agent.lab_extraction.registry import canonical_name, normalize_registered
from health_agent.pdf_lab_geometry import (
    _MAX_DRAWING_ITEMS,
    _MAX_DRAWINGS,
    extract_lab_geometry,
)

RUSSIAN_HEADERS = [
    "Параметр",
    "Значение",
    "Ед. измер.",
    "Реф.значение",
    "Представление",
]


def preamble_grid_pdf(*, headers=None, rows=None, competing=False, merged_at=None):
    headers = RUSSIAN_HEADERS if headers is None else headers
    rows = (
        rows
        if rows is not None
        else [
            ["Пролактин / Prolactin", "234", "мЕд/л", "80-400", "[-*-]"],
            ["Пролактин мономерный (пост ПЭГ)", "156", "мЕд/л", "60-300", "[---]*"],
        ]
    )
    body = [headers, *rows]
    if competing:
        body.append(headers if competing is True else competing)
    pdf = pymupdf.open()
    page = pdf.new_page(width=850, height=500)
    font = pymupdf.Font("cjk")
    page.insert_font(fontname="cyr", fontbuffer=font.buffer)
    xs = [20, 370, 460, 550, 660, 825]
    ys = [20 + 35 * index for index in range(len(body) + 4)]
    for x in (xs[0], xs[-1]):
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for index, x in enumerate(xs[1:-1], 1):
        bottom = ys[3] if merged_at == index else ys[-2]
        page.draw_line((x, ys[2]), (x, bottom))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    for row, value in (
        (0, "Synthetic report"),
        (1, "Collection date:"),
        (len(body) + 2, "Signature 2025-04-03 Prolactin 999 mU/L 1-1000"),
    ):
        page.insert_text((25, ys[row] + 21), value, fontname="cyr", fontsize=9)
    for row, values in enumerate(body, 2):
        for column, value in enumerate(values):
            page.insert_text(
                (xs[column] + 4, ys[row] + 21), value, fontname="cyr", fontsize=9
            )
    source = pdf.tobytes()
    pdf.close()
    return source


def test_preamble_grid_preserves_exact_source_cells_and_versions_evidence():
    source = preamble_grid_pdf()
    result = extract_lab_geometry(source, 1)
    assert len(result.rows) == 2
    assert result.method == "pdf_table_v2"
    assert result.source_sha256 == hashlib.sha256(source).hexdigest()
    assert [row.result.text for row in result.rows] == ["234", "156"]
    assert [row.unit.text for row in result.rows] == ["мЕд/л", "мЕд/л"]
    assert [row.reference.text for row in result.rows] == ["80-400", "60-300"]
    assert [row.comment.text for row in result.rows] == ["[-*-]", "[---]*"]
    assert "999" not in result.text
    assert "*" not in result.text


@pytest.mark.parametrize(
    "headers",
    [
        ["Параметр", "Значение", "Other", "Реф.значение", "Представление"],
        ["Параметр", "Значение", "Значение", "Реф.значение", "Представление"],
        ["Параметр", "Значение", "Реф.значение", "Ед. измер.", "Представление"],
    ],
)
def test_preamble_grid_rejects_inexact_headers(headers):
    assert extract_lab_geometry(preamble_grid_pdf(headers=headers), 1).rows == ()


@pytest.mark.parametrize(
    "options",
    [
        {"competing": True},
        {"merged_at": 1},
        {"merged_at": 2},
        {"merged_at": 3},
        {"merged_at": 4},
        {"competing": ["Test", "Result", "Reference range", "Unit", "Comment"]},
        {"rows": [["Unknown marker", "234", "мЕд/л", "80-400", ""]]},
        {"rows": [["Пролактин / Prolactin", "234", "g/L", "80-400", ""]]},
    ],
)
def test_preamble_grid_rejects_ambiguous_geometry_and_invalid_rows(options):
    assert extract_lab_geometry(preamble_grid_pdf(**options), 1).rows == ()


def test_prolactin_exact_aliases_and_distinct_literal_unit_families():
    assert canonical_name("Пролактин / Prolactin") == "prolactin"
    for alias in (
        "Пролактин мономерный (пост ПЭГ)",
        "Пролактин мономерный (пост-ПЭГ)",
        "Monomeric prolactin",
    ):
        assert canonical_name(alias) == "monomeric_prolactin"
    assert canonical_name("Macroprolactin").startswith("unmapped_")
    for name in ("prolactin", "monomeric_prolactin"):
        assert normalize_registered(name, "123", "мЕд/л")[1] == "mU/L"
        for unit in ("mU/L", "mIU/L", "uIU/mL", "ng/mL"):
            assert normalize_registered(name, "123", unit)[1] == unit


def test_second_exact_russian_header_maps_wrapped_reference_heading():
    source = preamble_grid_pdf(
        headers=[
            "Показатель",
            "Результат",
            "Ед. изм.",
            "Референсные\nпределы",
            "Комментарий",
        ],
        rows=[["Глюкоза", "5.2", "ммоль/л", "3.9-5.5", ""]],
    )
    result = extract_lab_geometry(source, 1)
    assert len(result.rows) == 1
    assert result.method == "pdf_table_v2"
    assert result.rows[0].reference.text == "3.9-5.5"


@pytest.mark.parametrize(
    "row",
    [
        ["Monomeric prolactin", "156", "60-300", "mIU/L", ""],
        ["Prolactin", "234", "80-400", "mU/L", ""],
    ],
)
def test_new_registry_rows_in_old_header_require_v2(row):
    result = extract_lab_geometry(gridded_pdf(rows=[row]), 1)
    assert len(result.rows) == 1
    assert result.method == "pdf_table_v2"


def test_preamble_grid_does_not_strip_numeric_flag():
    source = preamble_grid_pdf(
        rows=[["Пролактин / Prolactin", "234*", "мЕд/л", "80-400", "[---]*"]]
    )
    assert extract_lab_geometry(source, 1).rows == ()


def test_separate_physical_tables_on_one_page_are_independently_proven():
    source_pdf = pymupdf.open(stream=preamble_grid_pdf(), filetype="pdf")
    combined = pymupdf.open()
    page = combined.new_page(width=850, height=1000)
    page.show_pdf_page(pymupdf.Rect(0, 0, 850, 500), source_pdf, 0)
    page.show_pdf_page(pymupdf.Rect(0, 500, 850, 1000), source_pdf, 0)
    result = extract_lab_geometry(combined.tobytes(), 1)
    combined.close()
    source_pdf.close()
    assert len(result.rows) == 4
    assert result.method == "pdf_table_v2"
    assert result.rows[2].name.bbox[1] > result.rows[1].name.bbox[3]


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


def word_pdf(*, headers=None, rows=None, narrative=None, physical_grid=True):
    headers = headers or ["Test name", "Result", "Unit", "Reference range"]
    rows = rows or [["Glucose", "5.10", "mmol/L", "3.9-5.5"]]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=300)
    bounds = [30, 240, 330, 420, 570]
    xs = [35, 250, 340, 430]
    row_bounds = [30, 60, *[60 + 40 * i for i in range(1, len(rows) + 1)]]
    if physical_grid:
        for x in bounds:
            page.draw_line((x, row_bounds[0]), (x, row_bounds[-1]))
        for y in row_bounds:
            page.draw_line((bounds[0], y), (bounds[-1], y))
    for column, value in enumerate(headers):
        page.insert_text((xs[column], 48), value, fontsize=8)
    for index, row in enumerate(rows):
        y = 76 + index * 40
        for column, value in enumerate(row):
            values = value if isinstance(value, list) else [value]
            for line, fragment in enumerate(values):
                page.insert_text((xs[column], y + line * 12), fragment, fontsize=8)
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
            [["C-reactive", "protein"], "4", "mg/L", "0-5"],
            ["Glucose", "5.2", "mmol/L", "3.9-5.5"],
        ],
    )

    result = extract_lab_geometry(source, 1)

    assert [row.name.text for row in result.rows] == ["C-reactive protein", "Glucose"]
    assert result.rows[0].name.bbox[0] >= 30
    assert result.rows[0].name.bbox[2] <= 240
    assert result.rows[0].result.bbox[0] >= 240
    assert result.rows[0].result.bbox[2] <= 330
    assert result.rows[0].name.bbox[1] >= 60
    assert result.rows[0].name.bbox[3] <= 100


def test_kdl_rejects_multiple_result_words_and_does_not_consume_neighbor():
    source = word_pdf(
        rows=[
            ["Glucose", "5.1 5.2", "mmol/L", "3.9-5.5"],
            ["Hemoglobin", "145", "g/L", "130-170"],
        ]
    )
    result = extract_lab_geometry(source, 1)
    assert [row.name.text for row in result.rows] == ["Hemoglobin"]


def test_kdl_rejects_header_text_without_physical_column_and_row_proof():
    source = word_pdf(physical_grid=False)
    assert extract_lab_geometry(source, 1).rows == ()


def test_kdl_does_not_accumulate_adjacent_name_row_or_subheading():
    source = word_pdf(
        rows=[
            ["C-reactive", "", "", ""],
            ["Glucose", "5.2", "mmol/L", "3.9-5.5"],
        ]
    )
    result = extract_lab_geometry(source, 1)
    assert [row.name.text for row in result.rows] == ["Glucose"]


def test_kdl_rejects_word_bbox_crossing_physical_column_boundary():
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=180)
    bounds = [30, 240, 330, 420, 570]
    for x in bounds:
        page.draw_line((x, 30), (x, 100))
    for y in (30, 60, 100):
        page.draw_line((30, y), (570, y))
    for x, value in zip(
        (35, 250, 340, 430),
        ("Test name", "Result", "Unit", "Reference range"),
        strict=True,
    ):
        page.insert_text((x, 48), value, fontsize=8)
    page.insert_text((35, 78), "Glucose", fontsize=8)
    page.insert_text((315, 78), "5.1", fontsize=16)
    page.insert_text((340, 78), "mmol/L", fontsize=8)
    page.insert_text((430, 78), "3.9-5.5", fontsize=8)
    source = pdf.tobytes()
    pdf.close()

    assert extract_lab_geometry(source, 1).rows == ()


def test_kdl_rejects_excess_drawing_paths_with_safe_error():
    pdf = pymupdf.open(stream=word_pdf(), filetype="pdf")
    page = pdf[0]
    for index in range(_MAX_DRAWINGS + 1):
        y = 160 + (index % 100) * 0.01
        page.draw_line((580, y), (590, y))
    source = pdf.tobytes()
    pdf.close()

    with pytest.raises(ValueError, match="^invalid_pdf_geometry$"):
        extract_lab_geometry(source, 1)


def test_kdl_rejects_excess_nested_drawing_items_with_safe_error():
    pdf = pymupdf.open(stream=word_pdf(), filetype="pdf")
    page = pdf[0]
    shape = page.new_shape()
    for index in range(_MAX_DRAWING_ITEMS + 1):
        y = 160 + (index % 100) * 0.01
        shape.draw_line((580, y), (590, y))
    shape.finish()
    shape.commit()
    source = pdf.tobytes()
    pdf.close()

    with pytest.raises(ValueError, match="^invalid_pdf_geometry$"):
        extract_lab_geometry(source, 1)


@pytest.mark.parametrize(
    ("source_name", "source_unit", "expected"),
    [
        ("Средний объем эритроцитов (MCV)", "fL", "mcv"),
        ("Средняя концентрация Hb в эритроцитах (МСНС)", "g/L", "mchc"),
        ("Среднее содержание гемоглобина в эритроците (МСН)", "пг/кл", "mch"),
        ("Средний объем тромбоцитов (MPV)", "fL", "mpv"),
    ],
)
def test_allowed_aliases_are_exact_and_unit_compatible(
    source_name, source_unit, expected
):
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
