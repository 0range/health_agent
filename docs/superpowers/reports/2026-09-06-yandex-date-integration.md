# Yandex and medical-date integration

TL;DR: Yandex adapters and conservative medical-date recovery are merged. Combined synthetic suite: **974 passed**. Live archive: **7 dates recovered**, no lab observations approved. Yandex live acceptance is still pending credentials/synthetic smoke; no health data sent to Yandex.

## Code acceptance

- Yandex feature commits: `36266ad`, `61832c7`, `5257dd9`. Task review and final review approved after direct adapter coverage and cloud-disabled queue-lifecycle fixes.
- Date feature commits: `36c5a9e`, `d31837e`. Task and final review approved after invalid/future labelled evidence was made role-blocking.
- Final combined run: `uv run pytest -q` → 974 passed in 14.15 seconds; five inherited SWIG deprecation warnings.
- `uv run ruff check .` clean; `uv run mypy src` clean (110 source files); `git diff --check` clean.
- Date final-review nonblocking test suggestions: explicit existing-date chronology/stale ORM/review-row fixtures and successful CLI apply forwarding. Code paths reviewed as correct; no additional feature scope implied.

## Live date recovery

Default profile only. Preview: scanned 121, eligible 7, changed 0, blocked 0.
Explicit apply: scanned 121, eligible 7, changed 7, blocked 0.
Repeat preview: eligible 0, changed 0. Existing dates, lab review decisions and document processing/conflict statuses were not rewritten by recovery.

The broader audit found 15 collection-label candidates; only 7 meet the implemented strict field-boundary rule. The other documents remain undated. Study/readiness dates were not converted into collection or issue dates. Date repair alone does not make pending lab results verified or establish useful lab charts.

## Yandex setup in progress

Created dedicated `health-agent` folder and `health-agent-ai` service account with only the documented Responses roles (`ai.languageModels.user`, `ai.assistants.editor`). No network/compute resources provisioned. API key creation and synthetic live test are not yet accepted. Production provider and per-profile sharing are not enabled by these code merges.

Remaining product acceptance: see [five-scenario check](2026-09-06-scenario-acceptance.md). This report does not mark v0.1 or any full medical scenario complete.
