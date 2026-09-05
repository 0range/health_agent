# Telegram Russian UX independent re-review fixes

## Findings resolved

### P1: Locale-neutral emergency guidance

The urgent-care reply no longer names `112` or any other jurisdiction-specific number.
It tells the user to call their local emergency number or go to the nearest emergency
department. Detection order and the local-before-retrieval safety boundary are unchanged.

The safety regression requires the phrase `местному номеру экстренной помощи` and rejects
`112` in the response across the existing English and Russian red-flag cases.

### P2: Image receipt agreement

The image receipt now begins with the exact grammatically neutral sentence:

```text
Медицинское изображение получено и сохранено.
```

PDF keeps its masculine form: `Медицинский PDF-файл получен и сохранён.` The remaining
review and OCR guidance is shared and unchanged. An exact full-string image regression
protects both agreement and deterministic duplicate receipt replay.

## TDD evidence

Before implementation, the focused test run produced 16 expected failures and 11 passes:
15 parameterized urgent cases rejected the concrete number, and the image receipt rejected
the masculine wording.

After the two string-only fixes:

```text
uv run pytest tests/questions/test_safety.py \
  tests/questions/test_composition.py::test_telegram_image_inbox_imports_and_uses_stable_receipt \
  tests/questions/test_composition.py::test_telegram_pdf_inbox_imports_with_profile_provenance_and_is_replay_safe -q
28 passed, 5 warnings
```

## Full gates

```text
uv run pytest -q
784 passed, 5 warnings

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 91 source files

uv lock --check
Resolved 71 packages

git diff --check
clean
```

The warnings are the existing PyMuPDF/Swig deprecation warnings. No live service calls were
made. Citation, retry-byte, security, and profile-isolation behavior was not changed.
