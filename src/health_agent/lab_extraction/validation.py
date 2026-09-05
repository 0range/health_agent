"""Conservative extraction and shared validation of exact page evidence."""

import re
import unicodedata
from decimal import Decimal
from typing import Any

from health_agent.lab_extraction.registry import (
    bounded_decimal,
    canonical_name,
)
from health_agent.lab_extraction.types import (
    MAX_CANDIDATES,
    MAX_PAGE_CHARACTERS,
    Candidate,
    LocalResult,
)

_FIELDS = {
    "source_name",
    "source_value",
    "source_unit",
    "reference_text",
    "source_flag",
    "evidence_excerpt",
}
_ROW = re.compile(
    r"^\s*(?P<source_name>.+?)\s+(?P<source_value>[<>≤≥]?[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?)\s+"
    r"(?:(?P<source_flag>[HL↑↓*])\s+)?(?P<source_unit>\S+)(?:\s+(?P<reference_text>.*))?\s*$"
)
_METADATA = re.compile(
    r"^(?:patient|пациент|date|дата|collection|issued|birth|пол|sex|age|возраст|reference|референс)",
    re.IGNORECASE,
)


def _source_string(value: Any, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= limit
        or value != value.strip()
    ):
        raise ValueError("invalid_candidate_field")
    if any(
        unicodedata.category(char).startswith("C") and char not in "\n\t\r"
        for char in value
    ):
        raise ValueError("invalid_candidate_field")
    return value


def _field_span(field: str, excerpt: str, text: str, start: int = 0) -> tuple[int, int]:
    origin = text.find(excerpt)
    offset = excerpt.find(field, start)
    while offset >= 0:
        left, right = origin + offset, origin + offset + len(field)
        if (left == 0 or text[left - 1].isspace() or text[left - 1] == "|") and (
            right == len(text) or text[right].isspace() or text[right] == "|"
        ):
            return offset, offset + len(field)
        offset = excerpt.find(field, offset + 1)
    raise ValueError("candidate_evidence_mismatch")


def validate_candidates(payload: Any, text: str) -> tuple[Candidate, ...]:
    if not isinstance(payload, dict) or set(payload) != {"candidates"}:
        raise ValueError("invalid_candidate_schema")
    rows = payload["candidates"]
    if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
        raise ValueError("invalid_candidate_count")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError("invalid_candidate_schema")
        name = _source_string(row["source_name"], 120)
        value = _source_string(row["source_value"], 64)
        unit = _source_string(row["source_unit"], 32)
        excerpt = _source_string(row["evidence_excerpt"], 500)
        reference = row["reference_text"]
        flag = row["source_flag"]
        if flag is not None and flag not in ("H", "L", "↑", "↓", "*"):
            raise ValueError("invalid_candidate_flag")
        if reference is not None:
            reference = _source_string(reference, 120)
        if (
            excerpt not in text
            or any(field not in excerpt for field in (name, value, unit))
            or (reference is not None and reference not in excerpt)
            or (flag is not None and flag not in excerpt)
        ):
            raise ValueError("candidate_evidence_mismatch")
        name_span = _field_span(name, excerpt, text)
        value_span = _field_span(value, excerpt, text, name_span[1])
        unit_span = _field_span(unit, excerpt, text, value_span[1])
        if (
            not any(char.isalpha() for char in name)
            or re.fullmatch(r"[\s|:=]*", excerpt[name_span[1] : value_span[0]]) is None
        ):
            raise ValueError("candidate_evidence_mismatch")
        gap = excerpt[value_span[1] : unit_span[0]].strip(" \t\r\n|:=")
        if gap and gap != flag:
            raise ValueError("candidate_evidence_mismatch")
        if reference is not None:
            reference_span = _field_span(reference, excerpt, text, unit_span[1])
            gap = excerpt[unit_span[1] : reference_span[0]].strip(" \t\r\n|:=")
            if gap and gap != flag:
                raise ValueError("candidate_evidence_mismatch")
        if flag is not None:
            _field_span(flag, excerpt, text, value_span[1])
        qualified = value.startswith(("<", ">", "≤", "≥"))
        parsed = bounded_decimal(value[1:] if qualified else value)
        low, high = _reference_range(reference)
        result.append(
            Candidate(
                name,
                value,
                unit,
                reference,
                excerpt,
                canonical_name(name),
                None if qualified else parsed,
                flag,
                low,
                high,
            )
        )
    return tuple(result)


def _reference_range(reference: str | None) -> tuple[Decimal | None, Decimal | None]:
    if reference is None:
        return None, None
    match = re.fullmatch(
        r"\s*([+-]?[0-9]+(?:[.,][0-9]+)?)\s*[-–—]\s*([+-]?[0-9]+(?:[.,][0-9]+)?)\s*",
        reference,
    )
    if match is None:
        return None, None
    try:
        low, high = (bounded_decimal(value) for value in match.groups())
    except ValueError:
        return None, None
    return (low, high) if low <= high else (None, None)


def parse_local(text: str) -> LocalResult:
    if len(text) > MAX_PAGE_CHARACTERS:
        raise ValueError("page_text_limit")
    rows: list[Candidate] = []
    unresolved = not bool(text.strip())
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line or _METADATA.match(line):
            continue
        match = _ROW.fullmatch(re.sub(r"\s*\|\s*", " ", line))
        if match is not None:
            row = match.groupdict()
            # Cleaning table separators never fabricates evidence: exact original
            # source fields must still occur in the unchanged source line.
            row["evidence_excerpt"] = line
            reference = row["reference_text"]
            if reference:
                fields = reference.split()
                flags = [
                    token for token in fields if token in {"H", "L", "↑", "↓", "*"}
                ]
                if flags:
                    if (
                        row["source_flag"] is not None
                        or len(flags) != 1
                        or (fields[0] != flags[0] and fields[-1] != flags[0])
                    ):
                        unresolved = True
                        continue
                    row["source_flag"] = flags[0]
                    row["reference_text"] = (
                        reference[len(flags[0]) :].strip()
                        if fields[0] == flags[0]
                        else reference[: -len(flags[0])].strip()
                    ) or None
            try:
                candidates = validate_candidates({"candidates": [row]}, text)
            except ValueError:
                unresolved = True
            else:
                rows.extend(candidates)
            continue
        if any(char.isdigit() for char in line) and any(
            char.isalpha() for char in line
        ):
            unresolved = True
    if len(rows) > MAX_CANDIDATES:
        raise ValueError("page_candidate_limit")
    return LocalResult(tuple(rows), unresolved)
