"""Pure, bounded extraction from explicitly headered PDF laboratory tables."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

import pymupdf

from health_agent.lab_extraction.registry import canonical_name, normalize_registered

_METHOD = "pdf_table_v1"
_ERROR = "invalid_pdf_geometry"
_MAX_BYTES = 25 * 1024 * 1024
_MAX_PAGES = 100
_MAX_TABLES = 10
_MAX_ROWS = 200
_MAX_COLUMNS = 8
_MAX_WORDS = 10_000
_MAX_CELL = 500
_MAX_TEXT = 60_000
_MAX_CANDIDATES = 40
_NUMBER = re.compile(
    r"[<>≤≥]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?"
)

_GRID_HEADERS: dict[tuple[str, ...], tuple[str, ...]] = {
    (
        "Исследование",
        "Результат",
        "Референсные значения",
        "Ед. изм.",
        "Коммент",
    ): ("name", "result", "reference", "unit", "comment"),
    ("Test", "Result", "Reference range", "Unit", "Comment"): (
        "name",
        "result",
        "reference",
        "unit",
        "comment",
    ),
}
_WORD_HEADERS: dict[tuple[str, ...], tuple[str, ...]] = {
    (
        "Наименование исследования",
        "Результат",
        "Ед. измерения",
        "Норм. значения",
    ): ("name", "result", "unit", "reference"),
    ("Test name", "Result", "Unit", "Reference range"): (
        "name",
        "result",
        "unit",
        "reference",
    ),
}


@dataclass(frozen=True, slots=True)
class GeometryCell:
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class GeometryRow:
    name: GeometryCell
    result: GeometryCell
    unit: GeometryCell
    reference: GeometryCell
    comment: GeometryCell | None
    derived_line: str


@dataclass(frozen=True, slots=True)
class GeometryPage:
    page_number: int
    method: str
    rows: tuple[GeometryRow, ...]
    text: str
    source_sha256: str


def extract_lab_geometry(pdf_bytes: bytes, page_number: int) -> GeometryPage:
    """Extract only rows proven by one of the exact complete header layouts."""

    if (
        not isinstance(pdf_bytes, bytes)
        or not 0 < len(pdf_bytes) <= _MAX_BYTES
        or type(page_number) is not int
    ):
        raise ValueError(_ERROR)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
            if not 0 < len(document) <= _MAX_PAGES or not 1 <= page_number <= len(document):
                raise ValueError(_ERROR)
            page = document[page_number - 1]
            grid_rows = _grid_rows(page)
            rows = grid_rows if grid_rows else _word_rows(page)
    except Exception:  # noqa: BLE001 -- native parser details never cross this API
        raise ValueError(_ERROR) from None
    result = tuple(rows)
    if len(result) > _MAX_CANDIDATES:
        raise ValueError(_ERROR)
    text = "\n".join(row.derived_line for row in result)
    if len(text) > _MAX_TEXT:
        raise ValueError(_ERROR)
    return GeometryPage(page_number, _METHOD, result, text, digest)


def _grid_rows(page: pymupdf.Page) -> list[GeometryRow]:
    result: list[GeometryRow] = []
    finder = page.find_tables()
    if len(finder.tables) > _MAX_TABLES:
        raise ValueError(_ERROR)
    for table in finder.tables:
        data = table.extract()
        if not data or len(data) > _MAX_ROWS + 1:
            continue
        header = tuple(_text(value) for value in data[0])
        roles = _GRID_HEADERS.get(header)
        if roles is None or len(roles) > _MAX_COLUMNS or len(table.rows) != len(data):
            continue
        header_boxes = table.rows[0].cells
        if not _valid_mapping(header_boxes, page.rect):
            continue
        for values, source_row in zip(data[1:], table.rows[1:], strict=True):
            if len(values) != len(roles) or not _valid_mapping(source_row.cells, page.rect):
                continue
            cells = {
                role: _cell(values[index], source_row.cells[index])
                for index, role in enumerate(roles)
            }
            row = _accepted_row(cells)
            if row is not None:
                result.append(row)
    return result


def _word_rows(page: pymupdf.Page) -> list[GeometryRow]:
    words = page.get_text("words", sort=False)
    if len(words) > _MAX_WORDS:
        raise ValueError(_ERROR)
    bands = _bands(words)
    result: list[GeometryRow] = []
    for header_index, band in enumerate(bands):
        matched = _match_word_header(band)
        if matched is None:
            continue
        roles, header_cells = matched
        if not _valid_mapping([cell.bbox for cell in header_cells], page.rect):
            continue
        centers = [(cell.bbox[0] + cell.bbox[2]) / 2 for cell in header_cells]
        boundaries = [
            page.rect.x0,
            *[(left + right) / 2 for left, right in pairwise(centers)],
            page.rect.x1,
        ]
        body = bands[header_index + 1 :]
        pending_name: list[tuple] = []
        pending_bottom: float | None = None
        for body_band in body:
            if _match_word_header(body_band) is not None:
                break
            assigned: dict[str, list[tuple]] = {role: [] for role in roles}
            for word in body_band:
                center = (word[0] + word[2]) / 2
                column = next(
                    (i for i in range(len(roles)) if boundaries[i] <= center < boundaries[i + 1]),
                    None,
                )
                if column is not None:
                    assigned[roles[column]].append(word)
            populated = {role for role, values in assigned.items() if values}
            if populated == {"name"}:
                top = min(float(word[1]) for word in body_band)
                if pending_bottom is not None and top - pending_bottom > 20.0:
                    pending_name = []
                pending_name.extend(assigned["name"])
                pending_bottom = max(float(word[3]) for word in body_band)
                continue
            if not assigned["result"]:
                pending_name = []
                pending_bottom = None
                continue
            if pending_name:
                top = min(float(word[1]) for word in body_band)
                if pending_bottom is not None and top - pending_bottom <= 20.0:
                    assigned["name"] = [*pending_name, *assigned["name"]]
            pending_name = []
            pending_bottom = None
            cells = {
                role: _words_cell(values)
                for role, values in assigned.items()
                if values
            }
            row = _accepted_row(cells)
            if row is not None:
                result.append(row)
    return result


def _bands(words: list[tuple]) -> list[list[tuple]]:
    bands: list[list[tuple]] = []
    for word in sorted(words, key=lambda item: ((item[1] + item[3]) / 2, item[0])):
        center = (word[1] + word[3]) / 2
        if not bands or abs((bands[-1][0][1] + bands[-1][0][3]) / 2 - center) > 2.0:
            bands.append([])
        bands[-1].append(word)
    return [sorted(band, key=lambda item: item[0]) for band in bands]


def _match_word_header(
    band: list[tuple],
) -> tuple[tuple[str, ...], tuple[GeometryCell, ...]] | None:
    tokens = [_text(word[4]) for word in band]
    for phrases, roles in _WORD_HEADERS.items():
        expected = [phrase.split() for phrase in phrases]
        if tokens != [token for phrase in expected for token in phrase]:
            continue
        cells: list[GeometryCell] = []
        offset = 0
        for phrase, phrase_tokens in zip(phrases, expected, strict=True):
            selected = band[offset : offset + len(phrase_tokens)]
            cells.append(GeometryCell(phrase, _union(word[:4] for word in selected)))
            offset += len(phrase_tokens)
        return roles, tuple(cells)
    return None


def _accepted_row(cells: dict[str, GeometryCell]) -> GeometryRow | None:
    if not all(role in cells for role in ("name", "result", "unit", "reference")):
        return None
    name, value, unit, reference = (
        cells["name"].text,
        cells["result"].text,
        cells["unit"].text,
        cells["reference"].text,
    )
    if not all((name, value, unit, reference)) or _NUMBER.fullmatch(value) is None:
        return None
    canonical = canonical_name(name)
    if canonical.startswith("unmapped_"):
        return None
    raw = value[1:] if value.startswith(("<", ">", "≤", "≥")) else value
    try:
        normalize_registered(canonical, raw, unit)
    except ValueError:
        return None
    line = f"{name} | {value} | {unit} | {reference}"
    return GeometryRow(
        cells["name"],
        cells["result"],
        cells["unit"],
        cells["reference"],
        cells.get("comment"),
        line,
    )


def _valid_mapping(boxes: list[tuple | None], page: pymupdf.Rect) -> bool:
    if not boxes or any(box is None or not _valid_bbox(box, page) for box in boxes):
        return False
    concrete = [box for box in boxes if box is not None]
    return all(left[2] <= right[0] for left, right in pairwise(concrete))


def _valid_bbox(box: tuple, page: pymupdf.Rect) -> bool:
    return (
        len(box) == 4
        and all(math.isfinite(value) for value in box)
        and page.x0 <= box[0] < box[2] <= page.x1
        and page.y0 <= box[1] < box[3] <= page.y1
    )


def _cell(value: str | None, bbox: tuple | None) -> GeometryCell:
    assert bbox is not None
    return GeometryCell(
        _text(value),
        (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
    )


def _words_cell(words: list[tuple]) -> GeometryCell:
    ordered = sorted(words, key=lambda word: (word[1], word[0]))
    return GeometryCell(" ".join(_text(word[4]) for word in ordered), _union(word[:4] for word in ordered))


def _union(boxes: Iterable[tuple[float, ...]]) -> tuple[float, float, float, float]:
    values = [tuple(float(item) for item in box) for box in boxes]
    return (
        min(box[0] for box in values),
        min(box[1] for box in values),
        max(box[2] for box in values),
        max(box[3] for box in values),
    )


def _text(value: object) -> str:
    result = " ".join(str(value or "").split())
    return result if len(result) <= _MAX_CELL else ""
