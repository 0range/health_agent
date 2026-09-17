# Food continuity and weekly focus implementation plan

> Execute in the isolated worktree for this change. The selected user scope is
> food improvements 1 and 3 plus short help on request; no proactive coaching loop.

**Goal:** Preserve food events reliably and support one explicit, durable weekly focus.

**Architecture:** Keep FoodCoach as coordinator. Extract text decisions and focus
state into small modules backed by the existing Store. Keep source messages and
model estimates separate from confirmed user changes.

**Tech Stack:** Python 3.13, existing PostgreSQL/PilotStore, pytest, Telegram runtime.

## Global constraints

- Four meals; count calories without assigning a limit.
- Reminder spacing 30 minutes, limit three; no additional notifications.
- All records profile-scoped and idempotent by source_key.
- Ambiguous text must not silently overwrite the latest meal.
- Production examples and repair snapshots stay in ignored private `data/`.
- No new dependencies, no change to WHOOP, sleep or training goals, no release retag.

## Task 1: Preserve meal identity and separate text actions

Files: `src/health_agent/pilot/food.py`, `food_additions.py`, new
`food_conversation.py`, `tests/pilot/test_food_conversation.py`.

- [x] Write scenario regressions using MemoryStore and synthetic dates/meals:
  ```python
  coach.handle(profile, '/ел обед рис и рыба', source_key='lunch', now=now)
  # Deliver the due reminder, then describe a different food without a meal label.
  coach.handle(profile, 'удон с овощами', source_key='snack', now=later)
  assert len(store.list(profile, 'food', 'meal')) == 2
  coach.handle(profile, 'я же уже поел', source_key='ack', now=later)
  assert len(store.list(profile, 'food', 'meal')) == 2
  assert not coach.due(profile, later + timedelta(minutes=30))
  ```
- [x] Add separate acknowledgement/control records, conservative text routing,
  evidence reconciliation for mixed additions, and explicit user kcal provenance.
- [x] Verify additions preserve unrelated foods and consumed/planned distinction;
  check replay, expired reminders, cross-profile targets, and offline model fallback.
- [x] Run `uv run pytest tests/pilot/test_food*.py -q` and relevant Ruff/mypy checks.

## Task 2: A durable, explicitly chosen weekly focus

Files: new `src/health_agent/pilot/food_focus.py`, integration in `food.py`,
`runtime.py`, tests `tests/pilot/test_food_focus.py`, food runbook.

- [x] Test public command flow, restart and weekly output:
  ```python
  coach.handle(profile, '/фокус Полдник без готовки', source_key='focus', now=now)
  restarted = FoodCoach(store, brain)
  assert 'Полдник без готовки' in restarted.handle(profile, '/план', source_key='plan', now=now)
  restarted.handle(profile, '/фокус итог Не было времени', source_key='outcome', now=now)
  assert 'Не было времени' in restarted.handle(profile, '/неделя', source_key='week', now=now)
  ```
- [x] Implement a `FoodFocus(store)` object exposing `handle(profile, text,
  source_key, now, history) -> str | None`, `current(profile, now) -> Record | None`,
  and `summary(profile, now, history) -> str`. Store proposals separately from
  accepted focus events; accept only an existing unexpired proposal. Commands and
  results retain exact replay replies and never reactivate or extend older focus.
- [x] Render current focus in plan and breakfast feedback; weekly review only
  judges facts it can establish. Custom focuses use voluntary outcome, otherwise
  explicitly unknown. Offer one next step without silently replacing current focus.
- [x] Add short deterministic help for hunger, scheduling trouble and choosing
  food; no unsolicited follow-up or meal mutation. Test these with an active meal.
- [x] Run focus/food/runtime tests, then the pilot and Telegram regression suites.

## Task 3: Verify, repair confirmed historical errors, deploy

- [ ] Review diff for replay/provenance issues and public/private data separation.
- [ ] Before any production repair, write a private snapshot; repair only confirmed
  misbindings with their original timestamps, retain audit evidence, verify replay.
- [ ] Fast-forward tested work to main, restart only the food service, verify polling.
- [ ] Push reviewed commits to the existing GitHub remote and verify matching SHA.
- [ ] Report actual implemented behavior and validation; identify any remaining
  ambiguity without claiming universal understanding of natural-language messages.
