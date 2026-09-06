# Strict Lab Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Stop narrative/metadata false positives and consistently extract supported exact lab layouts through import and worker.

**Architecture:** One shared conservative text parser, with narrowly proven labelled/table layouts. Preserve original page text/excerpts. PDF geometry requiring a different evidence representation is separate, not smuggled into a relaxed validator.

**Tech Stack:** Existing Python parsers/registry, pytest, disposable PostgreSQL.

## Global Constraints

- Do not modify live data, reject/approve old observations, reset jobs/budgets, read credentials or call clouds. Root owns archive reconciliation after review.
- No guessed fields, changed source excerpts, cross-row matching, dropped inequality signs, unit conversion inference, broad field-order relaxation or automatic verification.
- Unknown analyte/unit pairs remain unresolved local input, not newly published junk. Original documents/text remain intact.
- Only listed lab/import files and their tests/docs; no migrations, shared CLI/config/panel changes. User requests autonomous parallel isolated implementation.

### Task 1: Shared strict parsing with exact-layout validation

**Files:** modify `src/health_agent/lab_extraction/validation.py`, `src/health_agent/labs.py`, and narrow `src/health_agent/importer.py` conversion only if needed; extend `tests/lab_extraction/test_validation.py`, `tests/test_labs.py`, `tests/test_importer.py`, worker tests; add `docs/lab-layouts.md`.

**Interfaces:** Keep parse_local(text)->LocalResult and validate_candidates(payload,text)->tuple[Candidate,...]. Add/use shared `parse_page_candidates(text)->LocalResult` if needed; importer `parse_lab_candidates(pages)` delegates to it per page instead of its divergent row grammar. Preserve LabCandidate's public field names; parsed_value may become Decimal|None to preserve qualified values, and source_flag optional trailing default if required. Import conversion must retain exact reference text, qualified token, page and source flag. Canonical naming must use existing registry consistently, not a second incomplete alias map.

Local emission requires exact registered analyte alias and `normalize_registered`-compatible unit; no prefix/substring analyte matching and no arbitrary word as unit. `parse_local('Order code 12345 status-text')` emits zero and remains unresolved. Unknown but potentially legitimate values stay unresolved for later cloud/operator handling. Known accepted rows are candidates requiring review, not verified. Preserve all supported one-line/flag/range/decimal/scientific/qualified behavior.

Add two explicit layouts, without guessing column identities:
1. Complete labelled record: `Показатель: Глюкоза\nРезультат: 5,1\nЕдиницы: ммоль/л\nРеференс: 3,9-5,5` (English Test/Analyte, Result, Unit/Units, Reference/Reference range equivalents; same-line labels allowed). At most8lines/1000characters, exact field labels/delimiters, one record; no arbitrary intervening prose. Missing unit/result, repeated competing labels, a second analyte or malformed field rejects that record. Reference optional only when no reference label is present.
2. Exact pipe/tab-separated record `Глюкоза | ммоль/л | 5,1 | 3,9-5,5`, in addition to existing name/value/unit/reference order. Require recognized exact name, compatible exact unit, bounded numeric/qualified token and optional source reference. Only these two explicit orders; no arbitrary permutation, duplicate result columns or header guessing. Extra columns reject except the narrowly preserved existing cloud layout `name | value | unit | standalone-known-flag | reference` (e.g. `Glucose | 5.1 | mmol/L | H | 3.9-5.5`). Full-suite integration discovered this valid existing contract; retain exact source/flag proof rather than deleting its test or allowing arbitrary five-column rows.

For cloud validation, add a narrowly bounded layout predicate that proves candidate fields equal the corresponding fields parsed from one of these complete exact excerpts. If neither grammar matches, retain the existing strict span-order path unchanged. Payload source fields must stay exact substrings of the unchanged excerpt/page; candidate-to-layout equality includes optional flag/reference, so model cannot reassign or omit a printed range to gain acceptance. No accepting a long multi-row excerpt based only on presence of its fields. Literal field-label syntax has no execution meaning.

- [ ] **Step 1: RED regression tests.** Metadata/protocol numeric prose and name-prefix pollution produce no local candidates; supported labelled and swapped columns accepted with exact source; mismatched values/fields, extra/repeated columns, two rows and unknown names/units remain rejected/unresolved. Existing forged evidence rejects.

```python
text = 'Показатель: Глюкоза\nРезультат: 5,1\nЕдиницы: ммоль/л\nРеференс: 3,9-5,5'
row = parse_local(text).candidates[0]
assert (row.source_name, row.source_value, row.source_unit) == ('Глюкоза','5,1','ммоль/л')
assert row.evidence_excerpt in text
assert not parse_local('Order code 12345 status-text').candidates
```

- [ ] **Step 2: Implement strict shared parser and layout proof.** Keep all numeric/storage bounds and cloud limits. Update tests whose old expectation explicitly published unknown local rows to the intended unresolved contract; do not blanket-skip failures or remove valid supported cases.
- [ ] **Step 3: Wire importer parity and tests.** Same invented page through initial importer and worker yields same canonical names/raw fields/references, pending review only. Narrative PDF emits zero; registered CBC/glucose can enter initial importer as well as baseline ferritin/B12. Qualified value preserves parsed_value=None, never15 for<15. Existing immutable page evidence untouched.
- [ ] **Step 4: Verify/commit.** Focused lab/import/worker tests, Ruff changed files, mypy src, diff-check. Report any valid legacy behavior lost or ambiguities. Root does final fullsuite and actual archive dry-run. No changes to EXTRACTOR_VERSION or old persisted jobs in this task.
