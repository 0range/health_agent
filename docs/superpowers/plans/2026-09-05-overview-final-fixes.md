# Overview Final Fixes Implementation Plan

> **For agentic workers:** Execute inline in this worktree; the controller explicitly prohibited nested agents for this final wave. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the four scoped overview defects and two native Sheets validation assertions without changing clinical persistence or external integrations.

**Architecture:** Add a pure presentation selector shared by prompt construction, citation validation, and cited-only footer rendering. Extend legacy evidence with optional source-display fields while leaving stored observations unchanged, and tighten the existing native Sheets boundary.

**Tech Stack:** Python 3.13, frozen dataclasses, SQLAlchemy, pytest, Ruff, mypy.

## Global Constraints

- No clinical database mutation or schema change.
- No new dependencies or live provider calls.
- Keep snapshot provenance immutable and complete internally.
- Run focused question/insight/Telegram composition/Sheets tests only; do not rerun the full suite.

---

### Task 1: Shared prompt and citation presentation

**Files:**
- Create: `src/health_agent/questions/presentation.py`
- Modify: `src/health_agent/questions/openai.py`
- Modify: `src/health_agent/questions/service.py`
- Test: `tests/questions/test_openai.py`
- Test: `tests/questions/test_service.py`

- [x] Add regression tests for priority-first selection, prompt-only citation validation, snapshot window semantics, cited-only references, and bounded overflow output.
- [x] Implement one deterministic selector that retains up to five attention signals, relevant gaps, then remaining signals up to thirty, exposing one bounded citation label per aggregate.
- [x] Use the selector in the prompt, validator, and footer; render only answer-cited entries and concise limitations.
- [x] Run focused question tests.

### Task 2: Faithful legacy lab display

**Files:**
- Modify: `src/health_agent/questions/models.py`
- Modify: `src/health_agent/questions/context.py`
- Modify: `src/health_agent/questions/openai.py`
- Modify: `src/health_agent/questions/service.py`
- Test: `tests/questions/test_context.py`

- [x] Add a real context-to-prompt regression for a qualified source value, differing normalized value/unit, printed reference, and absent reference.
- [x] Add backwards-compatible optional source value, source unit, and reference fields to evidence and propagate them through lab retrieval and relabeling.
- [x] Render and prompt from faithful source display fields, marking a missing lab reference explicitly unknown.
- [x] Run focused context and prompt tests.

### Task 3: Telegram-size and native Sheets edge regressions

**Files:**
- Modify: `tests/questions/test_question_loop_integration.py`
- Modify: `src/health_agent/google_sheets/api.py`
- Modify: `tests/google_sheets/test_api.py`

- [x] Add a synthetic large-context answer-to-Telegram split regression proving a short cited answer remains one 4096-character part.
- [x] Reject boolean `sheetId` metadata explicitly.
- [x] Assert an app-authored leading-equals cell is emitted as `stringValue`, never `formulaValue`.
- [x] Run focused Telegram composition and Sheets tests.

### Task 4: Review and handoff

**Files:**
- Modify: `.superpowers/sdd/2026-09-05-useful-overview/final-fix-report.md` in the central workspace.

- [x] Run focused pytest, Ruff, mypy, and diff/caller review.
- [ ] Commit the implementation and record exact evidence, commit SHA, and residual concerns in the central report.
