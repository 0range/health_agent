# Short weekly insights

`shared/settings/coaching-cycle.weekly_brief_enabled=true` selects one compact
main-bot message at the existing Sunday 18:00 Moscow slot. It uses the original
`cycle:weekly:YYYY-MM-DD` receipt, two-day catch-up and quiet-hour rules. It works
without an accepted plan and does not append a long proposal. `/неделя` or
`итоги недели` requests the latest Sunday-anchored brief; `/инсайты` retains the
detailed rolling report. `/цикл` still offers an explicitly accepted plan.

The calendar week is Monday–Sunday. A Sunday report is marked with its actual
local time, including available Sunday sleep and weigh-ins. Current-day food
entries remain visible but are excluded from the average for completed days.
Catch-up keeps the original week. The default detailed insight period is unchanged.

Goals are profile-scoped active/planned primary goals overlapping the relevant
week. The report freezes past-week and next-week goal snapshots separately.
`weekly_targets` supports `strength` and `padel`, integers 1–7 with total <=7;
these override the old generic workout-count fallback in new proposals.
Unaccepted proposals refresh after a goal edit, retaining prior snapshots. An
accept click that itself discovers an updated proposal asks to review that new
version. Accepted plans and previous deliveries remain immutable.

Typed activity counts mean distinct days by sport, not necessarily individual
sessions. Explicit COROS sport labels identify Strength or Padel; Tennis, generic
racket sports and swimming do not prove either goal type. Manual reports require
explicit type wording and reject negative/planned wording. Duplicate day/type
reports count once across COROS and manual sources. Missing entries never prove
that the user skipped a workout. This evaluates regularity, not measured strength.

The brief shows goal, weight limitation/trend, recorded-food estimate and coverage,
typed training, and one sleep observation when available. Exactly one suggestion
is selected and saved with a reason: establish the requested training rhythm;
adapt to explicit lack-of-time feedback; obtain missing weight/food observations;
test a supported dinner-timing association as a hypothesis; clarify portions when
recorded mean weight does not fall; otherwise retain the routine. These are local
coaching rules, not validated clinical thresholds. No calorie cap or load increase
beyond the chosen routine is invented. Body-weight change is not measured fat loss.

Frozen `shared/weekly_brief` contains text, version, full evidence, goal snapshots,
typed counts, data gaps and selected action. It is advice, not an accepted plan.
Retries return the identical text even if data or goals subsequently change.
No LLM call is needed for selecting numbers, wording or delivery.

The 2026-09-28 goal review records the user's requested 3–4 kg fat reduction with
strength preservation, without inventing a deadline or calorie target. The old
September primary goal is retained as superseded/paused, not achieved. The existing
October–November goal carries the explicitly chosen 2 strength + 1 padel rhythm.
Other health/life goals are retained. Actual measurements and update evidence stay
private under `data/verification/weekly-goal-brief-2026-09-28/`.
