# Weekly goal brief implementation plan

**Goal:** Deliver a concise goal-aware review and one suggested next-week change.

**Architecture:** Reuse deterministic joint observations and existing Sunday
delivery, adding an explicit calendar-period override and a separate compact
renderer. Structured goals feed both proposals and brief action selection.

**Tech stack:** Python 3.13, PilotStore/PostgreSQL, Telegram, pytest.

## Constraints

- Sunday 18:00 Moscow; existing receipt key and catch-up behavior.
- Goal: 3–4 kg fat reduction, no invented deadline or calorie budget.
- October–November: 2 strength + 1 padel per week, explicitly chosen by user.
- One suggestion; absent data does not establish failure or causality.
- Inline implementation; no delegated agents or manual Telegram test sends.

## Steps

- [ ] Add optional `through_date` to `build_insights` and `HealthInsights.build`;
  preserve default completed-day behavior. Test Sunday values, future exclusion
  and historical anchor: `assert r['through_date'] == '2026-09-27'` even on Monday.
- [ ] Add `weekly_goals.current_goals(store, profile, first, last)` and
  `training_targets(goals)`; use structured goal periods/targets in proposals.
- [ ] Add `weekly_brief.WeeklyBrief(store, insights).report(profile, now, sunday,
  key)` plus pure rendering/action helpers and durable shared/weekly_brief records.
  Test type/day deduplication and `assert text.count('➡️') == 1`.
- [ ] Wire compact weekly delivery behind `weekly_brief_enabled`, retaining the
  existing key and all legacy behavior when disabled. Add `/неделя` access in the
  main bot without requiring an accepted plan. Keep detailed `/инсайты` unchanged.
- [ ] Verify bounded output, profile/month isolation, receipt replay, failure
  safety and honest insufficient-data language. Run affected suites/Ruff/mypy.
- [ ] Prepare private audited goal/settings update and real-data preview. Preserve
  the old goal as paused/superseded; do not mark it achieved. Revise unaccepted
  upcoming proposals while retaining historical snapshots and accepted plans.
- [ ] Deploy from a clean main checkout, verify fresh polling, actual main-context
  goals and next delivery preview, push and verify GitHub HEAD. Record proof under
  ignored data/ with private permissions; remove only this task's worktree.
