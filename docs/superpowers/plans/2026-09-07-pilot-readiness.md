# Pilot Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkboxes. User authorized parallel work with disjoint ownership.

**Goal:** Complete existing healthcheck visibility and medical-import recovery, then deliver three evidence-grounded summaries.

**Architecture:** Extend local read-only panel readers/DTOs/rendering. Existing extraction service remains source of queue state and recovery controls. Root owns private operational processing, health evidence and Telegram delivery.

**Tech Stack:** Python, SQLAlchemy, existing Telegram SQLite state, pytest, local PostgreSQL.

## Global Constraints

- Approved completion scope: docs/superpowers/specs/2026-09-07-pilot-readiness-design.md, user explicitly requests these two completions and three summaries.
- No new service/schema, unrelated refactor, clinical values in panel, raw errors, tokens or cross-profile information. Read-only page: no external requests or filesystem/database creation while viewing status.
- Separate configured/bound from fresh bot polling. Fresh means aware timestamp with age 0..120 seconds; stale/future/missing/unknown is not healthy. Each bot's own state, profile binding required.
- Apple weight/workout candidates are a one-off import, not live sync, not deduplicated added COROS load. Display local date range/count only. COROS source dates are different from last sync timestamp.
- Queue counts are page processing jobs, lab review counts are observations. Show needs_attention separately. Do not silently green an incomplete queue.
- Preserve unrelated dirty docs/backlog.md and docs/research/. Root controls production writes and restart. Never commit private data or scratch reports.

## Task 1: Complete local status page

**Files:** Modify src/health_agent/panel/models.py, healthcheck.py, http.py, service.py. Add src/health_agent/panel/pilot_health.py if separating local pilot read responsibilities keeps healthcheck small. Tests tests/test_panel_healthcheck.py and optional tests/test_panel_pilot_health.py. Docs docs/healthcheck.md. No extraction/domain bot changes.

**Interfaces:** Existing HealthcheckSnapshot/HealthcheckProfile and HealthcheckReader.coverage stay backward compatible with optional/default DTO fields. Add immutable pilot status DTOs consumed by renderer. Production composition injects settings/paths and clock, test readers accept temporary paths/session factory; no live network.

- [ ] RED: test three bots with distinct fresh/stale/absent states and two profiles, future timestamp not healthy, token missing not an exception leak, no creation of missing state DB on GET.
- [ ] Implement local SQLite read-only queries from existing Telegram state schema (inspect stores.py; URI mode=ro), do not construct state wrapper if that initializes files. Bot identity must match own profile. Existing main connector can remain, but distinct labelled main/food/training cards show actual status. Only read private token metadata if needed; never expose token, and avoid requiring token payload where state identifies bot. Missing data explicitly unavailable.
- [ ] RED: synthetic PilotRecord activity/Apple rows and extraction jobs give isolated accurate count/date coverage, differentiate empty from query failure and queued/attention from verified-observation counts.
- [ ] Implement SQL aggregates scoped to profile/domain/kind, actual payload source event dates not insertion times, and latest sync/import metadata when present. Use safe parsing so malformed/future dates cannot become fresh source facts. Show Apple import limitation clearly; keep other source cards even on one read failure.
- [ ] Render concise Russian cards within existing layout, escape all text; Moscow time labelled explicitly on healthcheck. Existing route/host restrictions unchanged.
- [ ] GREEN: `.venv/bin/pytest -q tests/test_panel_healthcheck.py tests/test_panel_pilot_health.py` (omit absent optional file), `.venv/bin/ruff check src/health_agent/panel tests/test_panel_healthcheck.py tests/test_panel_pilot_health.py`, `.venv/bin/mypy src/health_agent/panel`. Include real disposable PostgreSQL and temp read-only SQLite coverage. Commit owned files only and report actual RED/GREEN commands/output.

## Root operations and handoff

- [ ] Read-only diagnosis of extraction code/state; any code defect gets a bounded additional TDD task before editing, no silent budget increases.
- [ ] Use existing recovery/run commands within current quota/consent; verify originals/candidates preserved and count outcomes accurately. No unknown-outcome retries without explicit authority.
- [ ] Query source facts for three private summaries; crosscheck all quoted dates/units, use comparable nonduplicate windows and disclose missing data. Browse primary clinical sources for recommendations, keep URLs in private evidence alongside concise messages.
- [ ] Review task1 spec/quality, run final whole-change review and full regression, activate panel only, verify HTTP and live values.
- [ ] Send exactly three requested messages through existing main TelegramMessenger with stable keys; store private text/evidence, do not claim unknown deliveries completed. Update docs and push feature branch.

## Task 2: Bounded extraction recovery

**Files:** src/health_agent/lab_extraction/{validation.py,types.py,models.py,cli.py,queue.py,service.py}, src/health_agent/ai/yandex.py only as needed; alembic/versions/0016_extraction_backfill_budget.py; focused extraction/Yandex/migration tests; docs/lab-extraction.md. No panel/bot/domain changes.

**Context:** User explicitly approved temporary daily budget ABOVE100 after asking; root will use500 then restore20. Existing20default unchanged. Actual cloud source_name collapses newline while evidence_excerpt is exact, failingstrictvalidation. Root diagnosesvia captured realoutput privately; use synthetic testcounterexample only, never copy PHI.

**Interfaces:** Public CLI/config accepts1..500 and DBconstraint agrees. RunReport/queueversion semantics remain compatible. Shared strict validate_candidates stays strict; add a separate narrowly bounded candidate source-name whitespace restoration before validation in cloud adapters. Candidate source strings afterrestoration retain exact originalsource spelling, notmodel-normalizedname. Optional run `document_id: UUID|None` and CLI --document-id allow bounded targetedrecovery; queuefilterstillownprofile/states/currentversion. Root can operate remainingbackfill without ad-hoc monkeypatch.

- [ ] RED budget501reject,500accept,default20,configurelowerdoesnotresetused; migrationup retainsconfig/data,down refuseswhen anyconfigured>100. Change existing modelnamedconstraint and addnewmigrationmatchingcurrenthead0015, no editoldmigrationhistory.
- [ ] RED synthetic page `Assay monomeric (post\nPEG)\n137\nmIU/L\n72 - 229`: model source_name `Assay monomeric (post PEG)` with exactexcerpt must safelyrecoverexactsource_name. Exactnormalinput unchanged. Ambiguous repeatedmatches, changednumeric/nameword/unit/reference, malformedexcerpt andforeignpage remainrejected. Restoreonly source_name whitespace; reference/value/unit unchanged. Capinput/outputwith existingbounds. Function must not turn invalidexcerpt into sourceevidence. Both actualYandex and OpenAI labadapter use consistent path, nonlabquestionresponses untouched. No automaticverifiedrows.
- [ ] Add safe granular reason only if necessary to distinguish evidence mismatch frombadJSON; preserveexistingpublicerrorcontracts ifnotneeded. Do not weaken strict validator/relaxwholepage evidence.
- [ ] Bump extractorversion for changed extraction behavior without re-enqueuing allcompletedpages/newattemptbudgets. Inspect existingversiondedup/retrylimits: ifbump indiscriminately rescanscompletedarchive, prefer explicitrevisiontrace preservingcurrentjobs anddocumentwhy. Original/rejected/verified data and lifetime3attemptfence remainintact; no resetoldattempts.
- [ ] RED targetedrun choosesownrequested documentonly, otherprofile/no matching no newcloudwork, normalrun unchanged. Implementfilterthroughservice/queueCLI withbounded1..20pages and0..10cloudcalls unchanged.
- [ ] GREEN focusedtests, Ruffchangedfiles,mypy; fullsuite rootafterbothworkers. Commitownedonly, reportactualcommands/RED/GREEN/output in workspace task-2-report.md (notGit). No liveAPI/DBmigration/config/retry/run; rootownsproductionoperations.
