# v0.1 bootstrap and launch implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkpoints below resume existing completed work, not a new architecture.

**Goal:** Deliver the four approved launch scenarios without universal parsing.

**Architecture:** Existing local PostgreSQL, immutable vault, review service, Sheets,
Metabase and Telegram. Bootstrap transcripts are source-attributed operator work,
not machine geometry and not permission to weaken automatic ingestion.

**Tech Stack:** Existing Python/SQLAlchemy, PyMuPDF, Yandex, Google and Telegram adapters.

## Global Constraints

- One owner profile; no new providers, Calendar authorization or second-user setup.
- Originals unchanged; private manifests mode0600 under ignored data/.
- Source-derived values, units, references and medical dates must be checked.
- Unknown dates stay unknown; initial transcription is not generic automatic extraction.
- User already approved this boundary and autonomous parallel work; no repeat approval gate.

### Task 1: Bootstrap evidence

Files: private `data/bootstrap-likely.json`, `data/bootstrap-probable.json`;
existing `data/archive-inventory-manifest.tsv` is the input.

- [ ] Inspect the17 likely lab documents, then triage26 probable candidates.
- [ ] Render original pages as needed and transcribe key blood markers only; each
  transcript includes source UUID/hash/page, exact printed fields, date evidence,
  visual-review status and original-page locator. Record unresolved/rejected pages.
- [ ] Independently compare transcripts with originals before publication.
- [ ] Reuse existing LabObservation/review normalization and profile checks for the
  one-time import; preserve source lineage and replay identity. No generic parser change.
- [ ] Verify database counts/dates, synchronize Sheets and existing charts, read back.

### Task 2: Useful question response

Files: `src/health_agent/ai/yandex.py`, `src/health_agent/config.py`,
`tests/ai/test_yandex.py`, private QA acceptance outputs.

- [ ] Retain existing concise presentation and strict internal citation validation.
- [ ] Select the existing-provider question model on actual bounded QA cases.
  If reasoning configuration is required, isolate it from lab extraction; defaults
  remain unchanged, no retries, timeout≤60seconds, output budget≤8000tokens.
- [ ] Test settings boundaries and independent QA/extractor requests with recorded
  synthetic clients; validate actual answer content separately from syntax.
- [ ] Activate only the accepted QA configuration and restart the existing bot.

### Task 3: Launch check and handoff

Files: `docs/backlog.md`, `docs/superpowers/reports/2026-09-06-v01-completion-status.md`.

- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src` after merges.
- [ ] Verify existing background source sync and synthetic incoming-document journey.
- [ ] Update the short backlog and evidence ledger to match the approved boundary.
- [ ] Push reviewed code on the current branch, deliver final Telegram test invitation,
  and report actual history coverage plus unresolved records without a100%claim.
