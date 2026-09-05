# Useful overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Deliver a trustworthy hybrid health snapshot and a multi-profile operational healthcheck in the existing application.

**Architecture:** Profile-scoped read-only projections reuse SQLAlchemy and existing status stores. The healthcheck remains separate from medical interpretation. Snapshot consumers must share deterministic classifications.

**Tech Stack:** Python, SQLAlchemy, existing stdlib HTTP panel, pytest, Ruff, mypy.

## Global Constraints

- No production credentials, medical values, or raw provider errors in test fixtures, logs, reports, or commits.
- No external calls on panel page reads; no database mutations from projections.
- Every health query is profile scoped. Empty/unconnected profiles must not inherit another profile's data.
- Russian user-facing text. Unknown is not healthy; configured is not running.
- Preserve existing user changes and existing APIs; no new infrastructure or dependencies.
- User explicitly authorized parallel work: independent worktrees override the skill's default serial implementer guidance.

### Task 1: Multi-profile operational healthcheck

**Files:** Create `src/health_agent/panel/healthcheck.py`, `tests/test_panel_healthcheck.py`; modify `src/health_agent/panel/http.py`, `src/health_agent/panel/service.py`, `src/health_agent/panel/models.py` only as needed; document in `docs/healthcheck.md`.

**Interfaces:** Consume existing PanelService profiles/cards, models and local Telegram heartbeat stores. Produce `GET /healthcheck` and a visible navigation link on the home/profile screens. Keep existing routes and injectable test services backwards compatible.

- [ ] Write failing tests with synthetic two-profile fixtures: WHOOP records and medical documents belonging to A never appear for B; B is explicitly unconnected. Assert source dates differ from sync dates and needs_review differs from verified.
- [ ] Add immutable healthcheck DTOs and read-only aggregate reader. Latest WHOOP date must come from actual time-series records, not merely a sync timestamp. Latest lab collection/issue dates must not fall back to import time; show received date separately where useful. Show pending extraction/review counts without clinical values.
- [ ] Render the existing seven connector statuses plus local data coverage for each profile. Distinguish Telegram user binding from the shared poller's recent heartbeat using existing status logic. Russian next steps for missing/auth/stale/error states, checked-at time, no automatic remote polling.
- [ ] Cover no profiles, DB/reader failure, HTML escaping, profile isolation, stale/unknown state, no credentials/raw errors. A failed per-profile source should degrade to unknown rather than crash the screen. Reuse host/security checks and prove GET does not mutate/call providers.
- [ ] Run focused tests, Ruff/mypy for touched code; run full suite once at final integration by controller. Commit code/docs and report commands/results and concerns.

### Task 2: Deterministic hybrid snapshot and grounded question context

**Files:** Create `src/health_agent/insights/__init__.py`, `models.py`, `service.py`, `catalog.py` and `tests/test_insights.py`; modify `src/health_agent/questions/context.py`, `models.py`, `openai.py` and relevant question tests. Do not edit panel, Sheets, dashboards, or extraction.

**Interfaces:** Produce `HealthSnapshotBuilder(session, clock=None).build(profile_id)` returning immutable snapshot with profile_id, as_of, attention, stable, gaps and structured signals with source citations. Consume existing Document/LabObservation and WHOOP ORM models. Wire the snapshot into HealthQuestionContext as an optional backwards-compatible field and into the existing OpenAI responder so it is useful through Telegram immediately. A later Sheets consumer will use the same builder.

- [ ] Write failing tests for verified source-value vs source reference (normalized conversion must not distort classification), absence of ranges, qualified results, nonfinite/inverted bounds, old dated labs, unverified exclusion, and profile isolation.
- [ ] Implement bounded latest-per-analyte/unit verified lab retrieval over history, explicitly dated and with source IDs/page and source range. Attention means outside source laboratory reference, stable means within supplied reference, gaps means insufficient quality/date/range; never infer diagnosis or a universal retest schedule.
- [ ] Test and compute seven complete UTC days against the preceding 28 days, requiring at least 14 distinct valid baseline days and at least four recent days. Group daily so duplicate entries cannot inflate sufficiency. Exclude naps and invalid/future wearable records. Show observed direction/relative change, not clinical abnormality or causality. Current weight must be clearly sync-as-of only, not a trend.
- [ ] Add a small versioned explanation catalogue, primary-source URLs supplied by controller in follow-up. Distinguish general educational knowledge from patient evidence and uncertain possible next steps. Never invent clinical targets. A missing catalogue entry yields a transparent generic explanation, not a fabricated claim.
- [ ] Wire optional snapshot into Q&A prompt preserving existing safety, citation guard and reply contracts. Latest lab context must no longer silently discard results older than 30 days. Existing labels remain valid; every newly supplied patient signal must cite existing context evidence or have supported labels included in the validator. Guard max prompt size and no raw document text.
- [ ] Run focused tests and Ruff/mypy for changes, self-review, commit and report. Controller owns one final integrated suite and live smoke, no production API calls in agent work.

## Controller integration checklist

- [ ] Diagnose first two cloud failures safely before further medical requests; preserve budget and no silent retries of unknown outcomes.
- [ ] Source catalogue links from primary medical/provider references.
- [ ] Review both tasks independently, merge, run one full regression suite and local GET/snapshot smoke.
- [ ] Record remaining work honestly: meaningful per-analyte Metabase charts, Sheets overview, extraction accuracy verification and full v0.1 scenario acceptance remain separate deliverables.
