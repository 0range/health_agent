# Local import date audit — 2026-09-06

## Outcome

The unknown dates are caused by a narrow deterministic grammar, not by missing local text and not by the paused cloud path. All 121 imported documents have both `collected_date` and `issued_date` unset. Every one has stored extracted text and at least one date-shaped token, but none matches the importer's exact label-plus-date expressions. The newer lab-extraction backfill deliberately extracts laboratory rows only; it ignores date metadata and has no path that updates document dates.

A small local-only follow-up is worthwhile. A conservative probe over predefined medical labels found one unambiguous, supported-format date candidate in 32 documents: 15 collection candidates and 17 issue/result candidates. This is evidence for implementation and tests, not authorization to update those documents. The other 89 documents should remain unknown until an explicitly supported label/layout or human review identifies the medical date.

## Root causes

1. `importer.py` accepts only ISO dates or day-first numeric dates with a four-digit year, immediately following a very small English/Russian label set (`src/health_agent/importer.py:99-108`). Common extra nouns such as “biomaterial”, “result”, or “study”, and intervening/reordered cells in extracted table text, prevent a match.
2. The parser returns a date only when exactly one distinct value matches a role; invalid and conflicting matches intentionally return `None` (`src/health_agent/importer.py:269-288`). This fail-closed behavior is correct and should remain.
3. Date inference runs during the original PDF/image import only (`src/health_agent/importer.py:149-158`, `src/health_agent/importer.py:190-198`). Gmail and Drive call `import_document` without supplying medical dates (`src/health_agent/gmail/medical_importer.py:75-87`, `src/health_agent/google_drive/medical_consumer.py:65-76`). Connector receipt/modified timestamps should not be substituted because they are not clinical dates.
4. The v1 local extraction service calls `parse_local` and publishes lab candidates only (`src/health_agent/lab_extraction/service.py:110-123`). `parse_local` explicitly skips lines beginning with date/collection/issued metadata (`src/health_agent/lab_extraction/validation.py:31-33`, `src/health_agent/lab_extraction/validation.py:154-163`), and its candidate schema contains no medical-date field. Queue publication updates page evidence, jobs, observations, and review state, but not `Document.collected_date` or `Document.issued_date` (`src/health_agent/lab_extraction/queue.py:320-390`). Consequently, successful local lab backfill cannot repair dates.
5. Focused tests protect one canonical English positive path indirectly and protect conflict/manual-review behavior, but do not exercise the real label variants, split table layout, role ambiguity, or a date-only backfill. The existing extraction integration tests correctly guarantee review-only lab observations and preservation of date conflicts.

## Aggregate evidence (PHI-free)

The database audit used `Settings`, `build_engine`, and a SQLAlchemy `Session` with `SET TRANSACTION READ ONLY`; it emitted aggregates only and rolled back. No provider was called.

| Measure | Count |
|---|---:|
| Documents | 121 |
| Missing both collection and issue date | 121 |
| Missing collection date | 121 |
| Missing issue date | 121 |
| Undated documents with stored digital/recovered text | 121 |
| Undated documents with empty stored text | 0 |
| Undated documents containing at least one currently supported date shape | 121 |
| Documents matching the current complete label-plus-date grammar | 0 |
| Documents with a unique high-confidence expanded collection candidate | 15 |
| Documents with a unique high-confidence expanded issue/result candidate | 17 |
| Documents with candidates for both roles in the conservative probe | 0 |
| Documents with multiple distinct candidates within either probed role | 0 |
| Documents without a high-confidence probed candidate | 89 |
| Pending lab observations | 584 |
| Verified lab observations | 0 |

Date-shape counts overlap: 109 documents contain a four-digit day-first numeric shape, 12 contain an ISO shape, 20 contain a month-name shape, and 6 contain a two-digit-year shape. Phrase-only matching also shows the layout gap: 24 documents contain a current collection-label phrase, yet zero satisfy the complete current expression; a supported date appears within the next 120 characters in 22 of those documents. Proximity alone is not sufficiently semantic to persist a medical date.

Current extraction state is consistent with this diagnosis: extraction is configured and enabled locally, cloud is disabled, with 0 queued, 267 waiting for cloud, 6 needing attention, 16 completed, and 0 cloud requests today. Re-enabling a provider would not fill document dates because neither local nor cloud lab candidate publication includes them.

## Safe next implementation scope

Keep this as a focused deterministic extension:

1. Extract the existing date logic into a pure, bounded parser returning evidence-backed candidates with `role`, parsed value, page number, and label family. Preserve four-digit-year parsing, calendar validation, per-role uniqueness, and fail-closed conflicts.
2. Add only explicit high-confidence label forms demonstrated by the aggregate probe: collection/drawing with optional biomaterial/sample wording, and issue/completion/result/study wording. Normalize benign whitespace and table separators, but require the date to remain in the same labeled field or a tightly defined adjacent cell. Do not use the first date in a document, filename/source metadata, arbitrary proximity, or birth/registration dates.
3. Reuse the parser at import and in a bounded, profile-scoped, idempotent local backfill over stored `DocumentPage` text. For existing documents, surface proposed dates for explicit review and apply them through the existing locked date-review transition; do not overwrite a populated role, clear conflicts automatically, change observation review status, or require OpenAI.
4. Add focused tests for the supported Russian/English variants, whitespace/table splits, duplicate identical values, invalid dates, two-digit years, month-name dates, birth dates, multiple conflicting role values, existing dates, profile isolation, retry idempotence, and the invariant that all lab observations remain untouched/pending.

Month-name and two-digit-year support should be a later explicit decision. Both are locally parseable, but locale and century policy must be specified before they can safely affect chart chronology.

## Verification

`uv run pytest -q tests/test_importer.py tests/test_labs.py tests/lab_extraction/test_local.py tests/lab_extraction/test_service.py` passed: 71 tests, with only existing dependency deprecation warnings.

No clinical data was mutated, no observation was approved, no provider was invoked, and no patient text, value, date, name, identifier, filename, credential, or raw database error was included in this report.
