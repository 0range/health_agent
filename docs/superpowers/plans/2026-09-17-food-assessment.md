# Meal assessment implementation plan

**Goal:** Short, personal meal feedback and honest daily calorie summaries.
**Architecture:** Pure food_assessment module, existing FoodCoach/store/history,
profile setting meal_assessment_version=1; legacy profiles unchanged.
**Tech stack:** Python, pytest, PostgreSQL, launchd, existing Telegram service.

## Constraints

Four meals as specified in the design; count calories without a target. No
inferred proportions, no double counting, unknown is not zero. No MyFitnessPal
writes or new scheduled messages. Run inline within the authorized scope.

## Tasks

- [x] Add tests/pilot/test_food_assessment.py for four meal rules, allowed treat,
  optional dinner vegetables, refined-vs-whole grain breakfast, unknown calories,
  correction replacement, same-day/profile scoping, repeated categories, and
  supper /сегодня summaries. Run tests before implementation.
- [x] Create src/health_agent/pilot/food_assessment.py with render_meal(analysis,
  category), calorie_total(history), daily_summary(history). Ground feedback in
  ingredient names; existing model feedback remains untrusted.
- [x] Route FoodCoach._feedback and one-day _summary to the new module when
  protocol.meal_assessment_version == 1. Preserve next-meal timing and controls,
  comment/portion/time correction paths, and reminder delivery.
- [x] Run focused tests, then pilot + Telegram regression suite, Ruff, mypy and
  git diff --check. Render private real records locally without sending messages.
- [x] Back up and update owner's protocol: current four plate rules, slow grains,
  optional dinner vegetables, permitted afternoon treat, no calorie target.
  Keep quiet hours/intervals and unrelated preferences. Restart food launchd;
  verify a poll newer than restart and error=None. Document and commit/push.
- [x] Document MyFitnessPal official API write scope and practical options,
  clearly separating supported automation from unverified internal endpoints.
- [x] Apply steering: optional breakfast protein, dinner yolk preference with
  uncertainty, stored aspiration without calorie limit; `/план` and weekly results
  with coverage, per-category composition counts, recurring issues and one focus.
  Test automatic weekly delivery and deduplication. 397 tests pass.

Owner settings updated with private backup; all existing meals unchanged. Live local preview used current saved data, no model call or Telegram message; fresh poll checked after bootstrap.
