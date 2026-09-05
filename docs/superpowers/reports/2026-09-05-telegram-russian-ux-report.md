# Telegram Russian UX review report

## Scope

This pass changes presentation only. Existing Telegram question, `/status`, `/sync`,
attachment import, and `/review` flows now use Russian user-facing copy. The existing
OpenAI safety prompt requests a Russian answer unless the user clearly requests another
language; changing only the response language is the sole instruction allowed from the
otherwise-untrusted question JSON.

No command, route, persistence field, dependency, schema, connector, or background job was
added. Technical CLI/log statuses and safe error codes remain unchanged and in English.

## User-visible outcomes

- Question failures, insufficient-evidence text, and urgent-care guidance are Russian.
- The deterministic footer uses Russian headings and explanations while preserving UTC
  timestamps, source citation labels, evidence order, and the per-source item cap.
- Fixed WHOOP metric labels, units, synchronization semantics, and the known weight-history
  limitation are Russian in the displayed evidence.
- Telegram data status uses Russian source names; sync guidance retains the exact existing
  local commands and bound profile UUID.
- Medical PDF/image receipts and the needs-attention fallback are Russian and retain their
  truthful review/OCR caveats.
- Review usage, prompts, empty state, failures, unavailable/already-resolved outcomes, and
  confirm/correct/reject acknowledgements are Russian. Slash commands and UUIDs are unchanged.

## TDD evidence

The focused tests were updated first. Against the old copy, the focused run failed on 49
English-text expectations and passed 56 unchanged behavioral assertions. After the minimum
implementation change, the same focused suite passed:

```text
uv run pytest tests/questions/test_service.py tests/questions/test_safety.py \
  tests/questions/test_openai.py tests/questions/test_composition.py \
  tests/questions/test_context.py tests/telegram/test_review.py \
  tests/telegram/test_capture_integration.py \
  tests/questions/test_question_loop_integration.py -q
105 passed, 5 warnings
```

The warnings are existing PyMuPDF/Swig deprecation warnings.

## Full verification

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

## Preserved contracts

The existing tests continue to cover citation fail-closed behavior, bounded/stateless OpenAI
requests, secret redaction, urgent handling before retrieval/model access, exact prepared
reply replay after 429 and restart, attachment receipt replay, at-most-once delivery outcomes,
and profile/chat/bot-scoped review and evidence retrieval. The implementation did not alter
these paths beyond their presentation strings.

No live Telegram, OpenAI, Gmail, WHOOP, or Google service call was made. Live rendering and
delivery remain unvalidated in this offline pass.
