# Real Lab Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development task by task. Root authorizes proceeding with the recommended safe design and local TDD when team slots are occupied; independent review remains a separate final gate.

**Goal:** Automatically extract review-only laboratory candidates from imported pages, with optional bounded profile-scoped cloud fallback and safe backfill/recovery.

**Architecture:** A self-contained `lab_extraction` package owns validation, extraction and durable page jobs; existing medical tables own facts/evidence/review. Automation invokes a separate bounded CLI worker after connectors.

**Tech Stack:** Python 3.13, SQLAlchemy/PostgreSQL, PyMuPDF, Apple Vision, official OpenAI Responses, Typer, pytest.

## Global Constraints

- Never use live APIs, credentials or personal records; tests are synthetic/offline.
- All new LabObservation rows are `needs_review`; retain exact per-value page evidence and never overwrite reviewed facts or dates.
- Local first; cloud text requires explicit per-profile enablement and uses existing Settings key/model, strict Responses schema, store=false and no retries.
- Bounds: 50 MiB original, 25M pixels, OCR30s, text60000 chars, cloud12000 chars, candidate excerpt500 chars, rows40/page, pages4/run default20 max, cloud2/run default10 max, daily20 default100 max, lifetime3 cloud attempts/page.
- Durable claims/request reservations before external work; unknown cloud outcomes require explicit retry acknowledgment. Never reset counters on retry.
- Worktree `health-agent-lab-extraction`, branch `codex/v1-real-lab-extraction`, base18d97a3; no merge/push. Add migration after0006 and coordinate renumbering with Sheets.
- Use apply_patch for authored edits; CLI/logs contain IDs/counts/safe codes only.

### Task 1: Extensible candidates, validation and local extraction

**Files:** Create `src/health_agent/lab_extraction/{__init__,types,registry,validation,local}.py`; extend `src/health_agent/labs.py` normalization through the registry; create `tests/lab_extraction/{__init__,test_validation,test_local}.py` and synthetic text fixtures.

**Interfaces:** `Candidate(source_name, source_value, source_unit, evidence_excerpt, reference_text, canonical_name, parsed_value)`; `parse_local(text) -> LocalResult(candidates, unresolved)`; `validate_candidates(payload, text) -> tuple[Candidate,...]`; `read_page(document_snapshot, page_number, vault_root, temporary_root) -> str`.

- [ ] RED tests: Russian CBC/chemistry and English thyroid/vitamins beyond old aliases; decimal commas, qualifier retention, unknown names/units, no evidence forgery, 500-character excerpt/40-row caps, no NaN/extreme values.
- [ ] Implement a declarative analyte alias/unit registry, conservative same-line parser and shared exact-substring validator. Only allow explicit normalization pairs; unknowns stay review-only and cannot be approved without mapping.
- [ ] RED local tests: valid PDF text, empty image requiring fake local OCR, SHA/path/profile snapshot mismatch, size/pixel limits, OCR failure/timeout, cleanup, nonempty stored text unchanged.
- [ ] Implement bounded vault reads and local rendering/OCR with fixed subprocess code, no shell and private temporary paths.
- [ ] GREEN focused tests and static checks, commit `feat: extract evidence-backed lab candidates locally`.

### Task 2: Strict bounded Responses fallback

**Files:** Create `src/health_agent/lab_extraction/openai.py`, `tests/lab_extraction/test_openai.py`.

**Interface:** `OpenAILabExtractor(settings, client=None).extract(profile_id, text) -> tuple[Candidate,...]`; `ExtractionError.safe_code` contains only a declared code.

- [ ] RED mock official responses.create arguments: strict text.format schema, store=false, configured model/output/reasoning, no tools, hashed profile identity, input cap.
- [ ] RED completed/refusal/incomplete/malformed/forged excerpt tests, exception redaction and zero automatic retries.
- [ ] Implement one-request text-only fallback; validate structured rows using Task1; do not trust model canonical names, dates or review status.
- [ ] GREEN focused tests and commit `feat: add bounded structured lab extraction fallback`.

### Task 3: Durable scoped queue, profile budgets and backfill

**Files:** Create `src/health_agent/lab_extraction/{models,service}.py`, `alembic/versions/0007_lab_extraction.py`, `tests/lab_extraction/{test_service,test_schema}.py`; update Alembic metadata imports and disposable cleanup table list.

**Interfaces:** `LabExtractionService(engine, settings, local_reader=None, cloud_extractor=None)` with configure, run, status, retry. Profile config stores enablement/daily reservation; page jobs store version/status/claim/attempt counters/source digest/method/model/safe error.

- [ ] RED disposable-DB tests for composite profile/page ownership, migration roundtrip, automatic page enqueue and bounded processing.
- [ ] RED replay/backfill after approved/rejected/corrected source; no duplicate observation/review, no page evidence replacement, profile isolation.
- [ ] Implement profile advisory lock and short transactional claim/result boundaries; page-only candidates plus ReviewItems atomically publish under document/page lock.
- [ ] RED cloud reservation/crash/restart/stale claim/daily/lifetime/per-run budget tests; explicit unknown retry acknowledgment and local restart recovery.
- [ ] GREEN focused tests and commit `feat: persist scoped lab extraction queue and budgets`.

### Task 4: CLI, scheduled composition, docs and final gates

**Files:** Create `src/health_agent/lab_extraction/cli.py`, `tests/lab_extraction/test_cli.py`, `docs/lab-extraction.md`, final report; minimally update main CLI registration, automation models/registry/ordering, README and automation tests.

- [ ] RED CLI configure/run/status/retry, invalid bounds, unknown-outcome acknowledgment and content-free failures.
- [ ] RED automation discovers configured profile jobs, invokes `lab-extract run PROFILE`, orders after sources and processes newly imported pages without connector changes/network.
- [ ] Wire the separate worker and document profile cloud opt-in, limits, waiting/attention/retry semantics, migration integration and live-only quality limitations.
- [ ] Run full offline pytest, Ruff, source+changed-test mypy and broader mypy with exact inherited-error reporting, offline lock, Alembic metadata/roundtrip and diff checks.
- [ ] Self-review, commit, generate independent full-branch review package; do not claim independent approval until obtained.
