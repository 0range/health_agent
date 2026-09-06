# Doctor Visits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Persist and prepare doctor visits with questions and an answer log, usable through Telegram and CLI before calendar authorization.

**Architecture:** Small local visit repository plus explicit Telegram commands. Reuse existing profile, timestamp and medical-source contracts; Calendar is a subsequent adapter, not required to save a visit.

**Tech Stack:** SQLAlchemy/Alembic, Typer, existing Telegram action protocol.

## Global Constraints

- Profile isolation on every read/write and composite foreign keys; no source document copying or rewriting, no inferred diagnosis/treatment.
- No live DB, credentials, browser or external calls. Tests use existing disposable database only.
- Root handles shared CLI/Telegram composition/panel registration and calendar adapter. Do not edit src/health_agent/cli.py, config.py, questions/* or panel/*.
- Only this task may change tests/conftest.py to add owned tables to disposable cleanup and alembic/env.py to import owned models. Preserve all existing cleanup safeguards.
- User explicitly authorizes autonomous parallel isolated implementation; user acceptance is at the end.

### Task 1: Profile-scoped visit record, notes and preparation

**Files:** create `src/health_agent/visits/{__init__,models,repository,preparation,telegram,cli}.py`, `alembic/versions/0010_doctor_visits.py`, `tests/visits/` and `docs/doctor-visits.md`; narrow registration imports in alembic/env.py and table cleanup additions in tests/conftest.py.

**Schema:** revision `0010_doctor_visits`, down `0008_lab_extraction` (parallel migration branch; root creates merge revision with recurrence after review). `health_visits`: id UUID, profile_id, unique(id,profile_id), public_code unique bounded32, title bounded200, starts_at/ends_at timezone-aware UTC, timezone_name, status planned/completed/cancelled CHECK, source_document_id nullable with same-profile composite FK, creation_key bounded200 unique, creation_fingerprint SHA256, created_at/updated_at. CHECK end>start. `health_visit_notes`: UUID, visit_id/profile_id composite FK, kind question/answer CHECK, text bounded10000, action_key bounded200 unique, created_at; immutable append-only notes. All user input validation before persistence, no arbitrary code execution. Downgrade refuses populated tables.

**Repository:** `VisitRepository(session)` exposes `create(profile_id, *, title, starts_at, ends_at, timezone_name, creation_key, source_document_id=None)`, `get(profile_id, code)`, `list(profile_id, limit=20)` (max100, upcoming first), `add_note(profile_id, code, *, kind, text, action_key)`, `notes(profile_id, code, limit=100)`, `complete(profile_id, code)`, `cancel(profile_id, code)`, `reschedule(profile_id, code, *, starts_at, ends_at, timezone_name)`. Return immutable dataclasses Visit/VisitNote, never detached ORM. Creation_key replay returns same visit only if original fingerprint matches; altered replay raises safe error. Note action replay returns same note only for matching profile/visit/kind/text; mismatches reject. Lock row for mutations. Completed/cancelled visits retain questions/answers; terminal states cannot silently reopen. No hard deletion. Notes remain appendable after completed visit, not cancelled. Bounded text/query limits.

**Preparation:** `prepare_visit(session, profile_id, code, *, now=None)` adds at most five general clinician questions once, using deterministic action keys from visit+question hash. Questions: what findings matter for this visit; what needs additional checking and why; how often to follow up; whether exercise/recovery affects the plan; what changes should prompt earlier contact. These are general discussion prompts, never personalized claims. Include a structured `VisitBrief` with visit, existing question/answer notes, and a bounded list of the owner's verified dated lab observations (max10) with exact original value/unit/reference and source/page links. Pending lab values are excluded and their count is clearly reported. Use the existing verified-history retrieval if appropriate; if it cannot preserve profile/date/source constraints, query the existing models directly. Do not copy medical facts into question text or mark them reviewed.

**Telegram:** `DatabaseVisitCommands(engine).handle(context,text)` fits existing TelegramTextActionService. Commands `/visits`, `/visit_new YYYY-MM-DDTHH:MM | title` (Europe/Moscow, default60min), `/visit CODE` (bounded detail), `/visit_prepare CODE`, `/visit_question CODE text`, `/visit_answer CODE text`, `/visit_done CODE`, `/visit_cancel CODE`, `/visit_move CODE YYYY-MM-DDTHH:MM`. Profile only from authenticated context. Creation/note keys derive from bot+update; route unknown commands as None; input errors short Russian help. Detail includes next usable commands, local time and clear distinction between question and logged answer. No claims that calendar event exists. Outputs bounded for messenger splitting (max12000 chars), no token/filepath leaks. Entry points exported for root composition; don't edit shared composition now.

**CLI:** export Typer `app` with create/list/show/prepare/note/complete/cancel commands, required --profile-id for every operation. Root registers `visit` group later. Setup and count-only errors never dump SQL/token exceptions; show/prepare intentionally display this user's visit data. CLI stores through the same repository.

- [ ] **Step 1: RED tests.** Create+same-key replay yields one row; altered/foreign replay rejected. Questions/answers append once per action key; isolation on every method and linked document; timezone/invalid interval/oversized inputs. Example:

```python
visit = repo.create(profile_id, title='Учебный визит', starts_at=start,
    ends_at=end, timezone_name='Europe/Moscow', creation_key='fixture-1')
repo.add_note(profile_id, visit.public_code, kind='answer', text='Учебная запись', action_key='note-1')
repo.add_note(profile_id, visit.public_code, kind='answer', text='Учебная запись', action_key='note-1')
assert len(repo.notes(profile_id, visit.public_code)) == 1
```

- [ ] **Step 2: Implement schema/repository/preparation.** Reuse existing date parsing (reject nonexistent/ambiguous explicit input), fixed validation and immutable source links. Test prepare replay, pending exclusion, real verified original-unit fixture and source-page provenance. Include migration roundtrip on empty disposable schema and populated downgrade refusal without destructive live access.
- [ ] **Step 3: Implement CLI and Telegram.** Fake bound MessageContext through actual repository: new→prepare→question→answer→done→read after new session; repeat same update and assert no duplicate. Foreign profile and malformed command cases. CLI integration invokes exported app with synthetic Settings/engine injection, no local credentials.
- [ ] **Step 4: Verify/commit.** Focused visits tests, adjacent affected tests, Ruff changed files, mypy src, diff-check. Root owns final full suite, branch merge migration and actual deployment. Report commands and RED/GREEN, no fake claim of live calendar success.
