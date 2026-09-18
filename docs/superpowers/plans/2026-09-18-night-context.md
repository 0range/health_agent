# Night context implementation plan

**Goal:** Save user-specified nights away and make their relevance visible to bots
and research exports without fabricating measurements or changing other profiles.

**Architecture:** Read bounded, profile-scoped PilotStore context records through
one small helper. Reuse it in PilotBrain and the daily research exporter. Keep raw
series unchanged and publish explicit per-sleep room-comparison metadata.

**Tech Stack:** Existing Python, PostgreSQL, JSON exports and pytest.

## Constraints

- Explicit Moscow night/wake dates; observation time stays separate.
- User plans remain plans; no assumed home presence when context is missing.
- No new notifications, calorie target, training prescription or research retag.
- Private snapshots precede database writes; public tests use synthetic notes.

## Tasks

- [ ] Add `pilot/sleep_context.py` with `night_contexts(store, profile, first, last)`
  returning sanitized dated annotations, and `sleep_comparisons(sleeps, contexts)`
  mapping each sleep's local end date to excluded-away or unknown room relevance.
- [ ] Add regression tests for date boundaries, invalid notes, profile isolation,
  future-plan provenance and unmatched sleeps. Extend brain tests for all three
  domains and the existing research export test to verify profile-separated notes.
- [ ] Pass the context through `pilot/brain.py` even in focused sleep answers;
  remove paused goals from the duplicate goal list in `pilot/sleep.py`.
- [ ] Export `night-context.json`, include it in the file hash manifest, and add
  `sleep_room_comparisons` in WHOOP context. Leave all measured rows unchanged.
- [ ] Run `uv run pytest tests/pilot tests/research tests/telegram -q`, Ruff and
  mypy on touched modules; inspect the diff for personal data.
- [ ] Snapshot and save only the confirmed user's notes; verify all three model
  contexts and private export files, then fast-forward main, restart conversation
  services, push GitHub and verify polling/remote SHA.

The broader product review is read-only: distinguish collection, useful local
features and the still-missing shared plan/execution/review loop.
