# Food Bridge Snack Implementation Plan

**Goal:** Record a small bridge snack with calories and remind about the still-pending full meal after 60–90 minutes.

**Architecture:** Keep durable meal records and delivery receipts; add a dedicated category and a small helper module for detection and timing. Enable only in the selected personal protocol.

**Tech Stack:** Python, existing FoodCoach, PostgreSQL PilotStore, pytest.

## Constraints

- Existing four-meal plan, quiet hours and reminder controls remain authoritative.
- A snack is not an extra completed main-meal slot.
- No personal chat, tokens or live verification payloads in git.
- Execute inline; deployment and GitHub publication already authorized.

## Task 1: Detection and durable scheduling

Files: `pilot/food_snacks.py`, `pilot/food.py`, `tests/pilot/test_food_snacks.py`.

- [x] Add regression journeys for breakfast → delayed lunch → nuts → lunch, explicit consumption time, replay, and photo after snack.
- [x] Add pure `recognizes(text, protocol)` and `next_category(payload)` helpers; protocol flag defaults off.
- [x] Persist `next_meal_category` with a snack, use 60-minute target and 150-minute delivery expiry, and preserve that category for the following full meal.
- [x] Exercise controls, three reminders, restart, early full meal, morning overlap, corrections and model failure.

Example contract: `assert recognizes('съел 10 орехов миндаля', {'bridge_snack': {'enabled': True}})`.
Run: `PYTHONPATH=src ../health-agent/.venv/bin/python -m pytest tests/pilot/test_food_snacks.py -q`.

## Task 2: Assessment, history and shared context

Files: `pilot/food_assessment.py`, `pilot/food_history.py`, `pilot/brain.py`, `pilot/runtime.py`.

- [x] Render snack acceptance and calories without applying the full plate checklist.
- [x] Include separate snack lines in day/week summaries, maintain four-slot count and add distinct snack/full-meal counts to shared history.
- [x] Project the selected rule into the main agent context and add `/перекус` help.
- [x] Verify 350 kcal breakfast + 70 kcal snack totals 420 kcal and only one main-meal slot.
- [x] Run relevant pilot/AI/Telegram tests, Ruff and mypy.

## Task 3: Verify and deploy

- [x] Run one bounded OpenAI analysis of ten almonds in an isolated store; verify next meal and calorie estimate without writing fake food into production.
- [x] Save private current protocol backup; activate the exception and replace contradictory blanket snack text atomically after checking unchanged state.
- [x] Fast-forward clean main, restart affected bot processes, verify fresh polling/no errors and push/compare remote commit.
- [x] Record evidence in a runbook and report archive-supported alternatives and limitations.
