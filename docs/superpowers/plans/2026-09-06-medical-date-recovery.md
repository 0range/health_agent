# Medical Date Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Repair missing medical dates without inventing chronology or overwriting reviewed data.

**Architecture:** A pure per-page parser is shared by initial import and a locked, bounded backfill. No new DB tables or dependencies. CLI defaults to dry run with aggregate-only output.

**Tech Stack:** Python, SQLAlchemy, Typer, pytest with disposable PostgreSQL.

## Global Constraints

- Never use Drive, email, import timestamps, birth dates, study dates or result readiness as substitutes.
- Existing dates and reviewed lab values stay untouched.
- No review/status/conflict clearing, no external calls and no lab approvals.
- Only modify the isolated worktree and synthetic test data; root owns any live dry run or apply.

### Task 1: Safe parser and bounded archive recovery

**Files:** Create `src/health_agent/medical_dates.py`, `tests/test_medical_dates.py`, `docs/medical-dates.md`; modify only date inference in `src/health_agent/importer.py` and add `review recover-dates` in `src/health_agent/cli.py`; extend `tests/test_importer.py` and CLI tests if needed.

**Interfaces:**
- `DateEvidence` frozen dataclass: `role: Literal['collected','issued']`, `value: date`, `page_number: int`, `start: int`, `end: int` offsets in original page string.
- `MedicalDates` frozen dataclass: `collected_date: date|None`, `issued_date: date|None`, `evidence: tuple[DateEvidence,...]`, `blocked_roles: frozenset[str]`.
- `infer_medical_dates(pages: Iterable[tuple[int,str]], *, today: date|None=None) -> MedicalDates`; default today is current UTC date, injectable in tests. Iterate pages separately, never concatenate fields across pages.
- Accept full four-digit-year ISO YYYY-MM-DD and consistent DD.MM.YYYY or DD/MM/YYYY with 1-2 digit day/month. Exact valid boundaries prevent longer tokens or partial dates. Reject invalid/future dates and differing values for the same role by blocking that role. Repeated identical evidence is not a conflict. Require collected <= issued when both are known; otherwise block both.
- Allowed collected labels: `дата забора`, `дата взятия`, optionally suffixed `биоматериала`, `материала`, `образца`; English `collection date`, `collected date`, `specimen date`. Allowed issued labels: `дата выдачи` optionally `результата` or `заключения`, legacy `дата заключения`; English `issue date`, `issued date`, `report date`. Case-insensitive. Explicitly exclude `дата исследования`, `дата готовности`, `дата выполнения`, `дата поступления`, birth/registration/order labels. Do not expand labels beyond this list.
- Same-field numeric value immediately follows label with horizontal whitespace and optional ':' or '-' separator. A newline is allowed ONLY when the label is the whole stripped line and the immediately following line is a date alone with optional HH:MM or HH:MM:SS. No blank lines, arbitrary proximity, competing labels or page crossing. Preserve original offsets even with CRLF. Longer words containing labels cannot match.
- `recover_document_dates(session: Session, *, profile_id: UUID, limit: int=200, apply: bool=False) -> dict[str,int]`: limit 1..500; select only this profile's documents with a missing date and deterministic id order; report scanned, eligible, changed, blocked counts. In apply mode lock/re-read each Document using populate_existing and its pages in the same transaction. Skip any `conflicting_medical_date` document completely. Fill NULL fields only with inferred unblocked dates, validate chronology against existing dates too. Never call human `set_document_medical_dates`, change safe_error/status/review rows or replace existing values. Dry run has no ORM mutations. Repeat apply cannot replace dates. No commit inside helper; caller owns transaction.
- Importer `_infer_medical_dates` delegates to parser with enumerated pages and preserves tuple-return compatibility; remove obsolete regex/date parser, no other importer behavior changes.
- `review recover-dates --profile-id UUID [--limit 200] [--apply]`: use standard Settings/engine/session_scope; print only stable aggregate counts, mode and safe error code. Do not print dates, text, filenames or raw exceptions. Profile ID is required. Default is dry-run.

- [x] **Step 1: Write failing pure-parser and backfill tests.** Include examples below, label-only+date-only next line, CRLF offsets, exact token boundaries, invalid leap date, future date, duplicate/conflicting pages, study-only date, unrelated next-line birth label, page-boundary split. For database fixtures create two profiles; assert dates/status/review untouched in dry run and foreign profile, existing dates preserved, conflicts not cleared, and idempotent apply.

```python
def test_extended_collection_label():
    found = infer_medical_dates([(1, 'Дата взятия биоматериала: 02.01.2000')])
    assert found.collected_date == date(2000, 1, 2)
    assert found.issued_date is None

def test_study_date_is_not_issue_date():
    found = infer_medical_dates([(1, 'Дата исследования\n02.01.2000')])
    assert found.collected_date is None
    assert found.issued_date is None
```

- [x] **Step 2: Record red.** `uv run pytest tests/test_medical_dates.py -q`.
- [x] **Step 3: Implement the pure parser, backfill and narrow wiring above.** No schema changes, heuristics or background jobs. The existing archive can be read later by root; do not inspect real patient text in agent output.
- [x] **Step 4: Test CLI dry-run/apply and integration.** Verify safe failure output, explicit profile requirement, null-only persistence after lock, parser use on initial PDF/text import, and that reviewed chart rows are not altered by existing/conflicting date recovery. Run focused tests then full suite once, Ruff, mypy, diff-check.
- [x] **Step 5: Document and commit.** Short TL;DR instructions for preview/apply, excluded roles and why missing dates are left empty. Stage only owned files. Report RED/GREEN commands and full results, no live calls/data changes.
