# Shared weekly coaching cycle and reliable retrospective meals

The user approved the proposed cycle: the bot offers one food task and a minimal
sports plan, the user accepts, recorded actions and wellbeing inform one shared
review and one next change. The user also reported new retrospective meal errors.
These are two bounded deliverables sharing a deployment, not a new health platform.

## Meal event identity

Explicit meal label and occurrence time outrank reminder context and message time.
Natural reports such as “Был завтрак в 08:15 …” start a meal; a short label correction
amends a recent pending report or the meal most recently recorded, retaining its
original time and food. Never create a meal from “Нет, завтрак” alone. Recognize
bounded common inflections/typos, ask on ambiguity, and avoid treating planned food
as consumed. Pending confirmations cannot replace the source description. A delayed
photo with an explicit time also anchors reminder time to the meal, not upload time.
Keep source text, capture time, correction provenance, replay identity and audit.
Repair only confirmed production mistakes after a private snapshot.

## Weekly state and interface

Use existing profile-scoped PilotStore records. A proposal has exact Monday–Sunday
dates, the applicable shared goals, travel notes, one food focus and a proposed
number of familiar training sessions (no prescribed pace, duration or working weight).
A proposal is not accepted work. The first proposal concerns the next calendar week.
Acceptance creates a dated shared plan and mirrors its focus/training target into
the existing domain records. Week/date-specific buttons avoid accepting a different
proposal through an old button. Simplifying changes the draft, never an accepted plan.
Users can record obstacles, self-reported completed sessions and a current weight;
all retain provenance. COROS facts are deduplicated; possible manual overlaps are not
silently summed. Missing data never means noncompliance or measured fat loss.

Main bot owns one Sunday 18:00 Moscow review plus next proposal, and one Wednesday
18:00 progress check for an accepted current plan. Startup delivers the initial
proposal during waking hours. Persisted notices prevent duplicates and bounded
catch-up handles downtime. Old independent weekly pushes are suppressed only for
profiles with this cycle enabled. Existing morning and meal reminders remain.

Commands/buttons work through PilotActions: `/цикл` (main `/неделя` alias), accept,
simplify, result/obstacle, manual training, weight, and stop. Main bot supplies reply
buttons. Food/training keep their specialist conversations; training plan commands
use the shared proposal when enabled. All bots receive the same accepted plan.

Reports describe recorded meals, completed training, subjective sleep and dated
weight observations. They explicitly distinguish unknown focus results, incomplete
logs, stale weight and travel. A short result/obstacle question and one next step
complete the loop. No new calorie target, medical claim, race plan or external write.

## Acceptance

Retrospective breakfast/lunch/afternoon remain separate and correctly timed even if
reported out of order; short corrections, source replay and pending confirmation
preserve identity. Weekly acceptance survives restart and replay without date drift;
other profiles and expired buttons cannot alter it. Runtime scheduling delivers one
shared review, retains ordinary reminders and uses truthful data boundaries.
