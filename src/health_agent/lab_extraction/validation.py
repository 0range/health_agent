"""Conservative extraction and shared validation of exact page evidence."""

import re
import unicodedata
from decimal import Decimal
from typing import Any

from health_agent.lab_extraction.registry import (
    bounded_decimal,
    canonical_name,
    normalize_registered,
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
    r"^\s*(?P<source_name>.+?)(?:\s*[:=]\s*|\s+)(?P<source_value>[<>≤≥]?[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?)\s+"
    r"(?:(?P<source_flag>[HL↑↓*])\s+)?(?P<source_unit>\S+)(?:\s+(?P<reference_text>.*))?\s*$"
)
_METADATA = re.compile(
    r"^(?:patient|пациент|date|дата|collection|issued|birth|пол|sex|age|возраст|reference|референс)",
    re.IGNORECASE,
)
_VALUE = re.compile(
    r"[<>≤≥]?[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_LABEL_NAMES = {
    "test": "name",
    "analyte": "name",
    "показатель": "name",
    "result": "value",
    "результат": "value",
    "unit": "unit",
    "units": "unit",
    "единица": "unit",
    "единицы": "unit",
    "reference": "reference",
    "reference range": "reference",
    "референс": "reference",
    "референсный интервал": "reference",
}
_LABEL = re.compile(
    r"(?P<label>reference\s+range|референсный\s+интервал|test|analyte|"
    r"показатель|result|результат|units?|единиц(?:а|ы)|reference|референс)\s*:\s*",
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
        excerpt = _source_string(row["evidence_excerpt"], 1000)
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
        explicit = _explicit_layout_fields(excerpt)
        if explicit is not None:
            if not _complete_explicit_excerpt(excerpt, text):
                raise ValueError("candidate_evidence_mismatch")
            expected = {
                "name": name,
                "value": value,
                "unit": unit,
                "reference": reference,
                "flag": flag,
            }
            if explicit != expected:
                raise ValueError("candidate_evidence_mismatch")
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
            continue
        if "|" in excerpt or "\t" in excerpt:
            raise ValueError("candidate_evidence_mismatch")
        if _LABEL.search(excerpt) is not None:
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
        flag_between = flag is not None and gap == flag
        if gap and gap != flag:
            raise ValueError("candidate_evidence_mismatch")
        tail_end = unit_span[1]
        if reference is not None:
            reference_span = _field_span(reference, excerpt, text, unit_span[1])
            tail_end = reference_span[1]
            gap = excerpt[unit_span[1] : reference_span[0]].strip(" \t\r\n|:=")
            flag_between = flag_between or (flag is not None and gap == flag)
            if gap and gap != flag:
                raise ValueError("candidate_evidence_mismatch")
        if flag is not None:
            flag_span = _field_span(flag, excerpt, text, value_span[1])
            tail_start = reference_span[1] if reference is not None else unit_span[1]
            if not flag_between and (
                flag_span[0] < tail_start
                or re.fullmatch(r"[\s|:=]*", excerpt[tail_start : flag_span[0]]) is None
            ):
                raise ValueError("candidate_evidence_mismatch")
            if flag_span[0] >= tail_start:
                tail_end = flag_span[1]
        if re.fullmatch(r"[\s|:=]*", excerpt[tail_end:]) is None:
            raise ValueError("candidate_evidence_mismatch")
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
    labelled = _parse_labelled(text)
    if labelled is not None:
        try:
            candidates = validate_candidates({"candidates": [labelled]}, text)
        except ValueError:
            return LocalResult((), True)
        return LocalResult(candidates, False)
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line or _METADATA.match(line):
            continue
        explicit = _parse_pipe(line) if "|" in line or "\t" in line else None
        if "|" in line or "\t" in line:
            if explicit is None:
                unresolved = True
                continue
            row = explicit
            match = None
        else:
            match = _ROW.fullmatch(line)
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
        if match is not None or explicit is not None:
            try:
                candidates = validate_candidates({"candidates": [row]}, text)
                candidate = candidates[0]
                _require_registered(candidate)
            except ValueError:
                unresolved = True
            else:
                rows.append(candidate)
            continue
        if any(char.isdigit() for char in line) and any(
            char.isalpha() for char in line
        ):
            unresolved = True
    if len(rows) > MAX_CANDIDATES:
        raise ValueError("page_candidate_limit")
    return LocalResult(tuple(rows), unresolved)


def parse_page_candidates(text: str) -> LocalResult:
    """Shared strict page parser used by initial import and queued extraction."""

    return parse_local(text)


def _require_registered(candidate: Candidate) -> None:
    if candidate.canonical_name.startswith("unmapped_"):
        raise ValueError("unsupported_lab_name")
    raw = (
        candidate.source_value[1:]
        if candidate.source_value.startswith(("<", ">", "≤", "≥"))
        else candidate.source_value
    )
    normalize_registered(candidate.canonical_name, raw, candidate.source_unit)


def _parse_pipe(excerpt: str) -> dict[str, str | None] | None:
    if "\n" in excerpt or "\r" in excerpt:
        return None
    delimiter = "|" if "|" in excerpt else "\t"
    if delimiter == "|" and "\t" in excerpt:
        return None
    fields = [field.strip() for field in excerpt.split(delimiter)]
    if len(fields) not in {3, 4} or any(not field for field in fields):
        return None
    name = fields[0]
    if _VALUE.fullmatch(fields[1]):
        value, unit = fields[1], fields[2]
    elif _VALUE.fullmatch(fields[2]):
        unit, value = fields[1], fields[2]
    else:
        return None
    reference = fields[3] if len(fields) == 4 else None
    reference, flag = _reference_and_flag(reference)
    result: dict[str, str | None] = {
        "source_name": name,
        "source_value": value,
        "source_unit": unit,
        "reference_text": reference,
        "source_flag": flag,
        "evidence_excerpt": excerpt,
    }
    return result


def _reference_and_flag(reference: str | None) -> tuple[str | None, str | None]:
    if reference is None:
        return None, None
    fields = reference.split()
    flags = [token for token in fields if token in {"H", "L", "↑", "↓", "*"}]
    if not flags:
        return reference, None
    if len(flags) != 1 or (fields[0] != flags[0] and fields[-1] != flags[0]):
        return reference, None
    flag = flags[0]
    remaining = (
        reference[len(flag) :].strip()
        if fields[0] == flag
        else reference[: -len(flag)].strip()
    )
    return remaining or None, flag


def _parse_labelled(text: str) -> dict[str, str | None] | None:
    excerpt = text.strip()
    if not excerpt or len(excerpt) > 1000 or len(excerpt.splitlines()) > 8:
        return None
    found: dict[str, str] = {}
    for line in excerpt.splitlines():
        position = 0
        while position < len(line):
            match = _LABEL.match(line, position)
            if match is None:
                return None
            next_match = _LABEL.search(line, match.end())
            end = len(line) if next_match is None else next_match.start()
            value = line[match.end() : end].strip()
            key = _LABEL_NAMES[" ".join(match.group("label").casefold().split())]
            if not value or key in found:
                return None
            found[key] = value
            position = end
    if set(found) not in ({"name", "value", "unit"}, {"name", "value", "unit", "reference"}):
        return None
    if _VALUE.fullmatch(found["value"]) is None:
        return None
    return {
        "source_name": found["name"],
        "source_value": found["value"],
        "source_unit": found["unit"],
        "reference_text": found.get("reference"),
        "source_flag": None,
        "evidence_excerpt": excerpt,
    }


def _explicit_layout_fields(excerpt: str) -> dict[str, str | None] | None:
    row = _parse_pipe(excerpt) if "|" in excerpt or "\t" in excerpt else _parse_labelled(excerpt)
    if row is None:
        return None
    candidate = Candidate(
        str(row["source_name"]),
        str(row["source_value"]),
        str(row["source_unit"]),
        row["reference_text"],
        excerpt,
        canonical_name(str(row["source_name"])),
        None,
        row["source_flag"],
    )
    try:
        _require_registered(candidate)
    except ValueError:
        return None
    return {
        "name": row["source_name"],
        "value": row["source_value"],
        "unit": row["source_unit"],
        "reference": row["reference_text"],
        "flag": row["source_flag"],
    }


def _complete_explicit_excerpt(excerpt: str, text: str) -> bool:
    """Prove that an explicit excerpt covers complete physical source records."""

    start = text.find(excerpt)
    while start >= 0:
        end = start + len(excerpt)
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
        whole_lines = not text[line_start:start].strip() and not text[end:line_end].strip()
        if whole_lines:
            if "|" in excerpt or "\t" in excerpt:
                return "\n" not in excerpt and "\r" not in excerpt
            previous = text[:line_start].rstrip("\r\n").rsplit("\n", 1)[-1].strip()
            following = text[line_end + 1 :].split("\n", 1)[0].strip()
            if not (
                (previous and _LABEL.match(previous))
                or (following and _LABEL.match(following))
            ):
                return True
        start = text.find(excerpt, start + 1)
    return False
