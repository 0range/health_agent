# Shared weekly coaching cycle

The main bot proposes a dated Monday–Sunday plan: one food focus and a chosen
number of familiar short workouts. Only explicit acceptance activates the plan.
Food and training receive the same accepted plan; morning sleep check-ins provide
subjective wellbeing. Calorie counting remains without a daily target.

## User flow

- `/цикл` in any bot (or `/неделя` in the main bot) shows the current accepted
  plan, or a draft for next week. The main bot offers dated accept/simplify buttons.
- `Принять неделю DD.MM.YYYY` accepts that exact current/upcoming proposal;
  `Упростить неделю DD.MM.YYYY` reduces an unaccepted draft to one workout and
  a smaller food task. The changed draft still needs acceptance.
- Training `/план` uses the shared proposal; `/сохранить план` accepts the upcoming
  draft. An old or missing draft is never silently replaced and accepted.
- `/цикл итог <текст>` records a result or obstacle against the current accepted
  week (previous week if no current plan). `/цикл итог` shows the factual review.
- `/тренировка YYYY-MM-DD <что сделал>` records a completed activity on a date
  in the past 14 days; `/вес <кг>` records current self-reported weight.
- `/цикл стоп` stops shared pushes; `/цикл вкл` re-enables them. History and
  accepted tasks remain. Domain weekly pushes do not resume automatically.

The initial draft arrives once during 08:00–21:00 Moscow. Wednesday at 18:00 the
main bot checks an accepted current plan (one-day catch-up); Sunday at 18:00 it
reviews the accepted week as of delivery and proposes the next (two-day catch-up).
Sunday evening is a partial-week cutoff, not a claim that the entire day has ended.
Ordinary morning and food reminders continue. Domain summaries remain available
on demand. A stopped or unaccepted plan is not reported as completed work.

## Records and evidence

Owner opt-in is `shared/settings/coaching-cycle`: `enabled: true`,
`replace_domain_weeklies: true`, `training_sessions: <user preference>`.
Create this only for an authorized profile; it is not a global default.
`cycle_proposal` and `cycle_plan` use Monday's ISO date as their source key.
Acceptance mirrors the exact dates into `food/focus` and `training/accepted_plan`.
A future food focus does not hide today's applicable focus. Input results and
replies are durable and scoped to profile and Telegram source key.

Reviews use period-scoped food history, unique COROS activities, manually reported
workout days, subjective rested scores and dated weight observations. Manual reports
count at most once per day and are not added to COROS counts on that date; this can
undercount distinct same-day sessions. Missing records mean unknown, not failure.
Food-focus success requires the user's report when logs cannot establish it.
Weight observations do not establish fat loss. No intensity or duration is prescribed.
A reported time obstacle can simplify the next proposal, never an accepted plan.
Away-night context informs proposals and keeps bedroom air separate from away sleep.

Stable keys `cycle:welcome`, `cycle:checkin:<Monday>` and `cycle:weekly:<Sunday>`
are dispatched through the main bot's existing frozen outbound/delivery receipt
mechanism. Retrying after a failure or restart does not duplicate delivery.

## Meal event corrections

Explicit unambiguous meal labels and HH:MM times outrank reminder context. Common
label inflections and the typo “полдний” are recognized. A short label correction
binds to a fresh pending description or most recently captured meal, retaining food
and occurrence time. Empty label messages are not new meals. Explicit-time photos
anchor reminders to the meal time; same-meal follow-up photos still group normally.
Source capture times and raw messages remain separate from meal occurrence times.
The existing time parser remains bounded to a plausible same-day HH:MM; ambiguous
or out-of-window dates require clarification.

Historical repairs require a private snapshot, a cloned-store preview, source-based
assertions and an atomic transaction checking unchanged preimages. Keep repair
scripts, chat text and snapshots under ignored `data/` with private permissions.
Supersede false records instead of deleting raw evidence. Replays must not create
new meals or model calls.

## Validation and rollout

Run `uv run pytest tests/pilot tests/research tests/telegram -q`, Ruff and mypy.
After fast-forwarding tested code, restart only the three conversation launch agents;
research collectors continue. Check fresh poll timestamps/error fields, main
`cycle:welcome` delivery receipt, owner-only settings and absence of an accepted
plan until the user presses accept. Inspect exact repaired meal times and preserved
comments via `build_food_history`; verify source replay record counts are unchanged.
Push the tested commit and verify the remote SHA. Do not move the existing v0.1 tag.
