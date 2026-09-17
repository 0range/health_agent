# Meal assessments and daily calories

Implement the user's four-meal plan in the existing food bot. Each meal result and
correction gets a short Russian assessment with emoji, approximate meal calories,
and the total of logged calories for its Moscow calendar date. Breakfast: slow
carbohydrate source and fruit; lunch: Harvard plate components plus fruit;
afternoon: Harvard components and a treat; dinner: protein, optional vegetables.
Treats are allowed at afternoon; their absence is not a reason to push more food.
Presence of components cannot establish plate proportions. Missing items are
reported as absent from the record, not proof they were not eaten. Model prose
must not override saved user rules. Optional vegetables never become mandatory.

User clarified: count calories only, no daily target or invented calorie limits.
Unknown calories stay unknown; partial totals disclose missing estimates.
Corrections replace existing values, never add a second version. Count distinct
meal categories when describing coverage of four planned meals. Moscow day
boundaries and profile isolation apply. Supper response and /сегодня include a
short per-meal breakdown. No new scheduled daily message is implied.

Use a small deterministic presentation module and a versioned profile setting;
retain existing rendering for other profiles. Source estimates, comments, photo
revisions, reminders and stored event times keep their existing contracts.
Default profile gets an audited settings update, without re-running old photos.
No manual test Telegram messages. Restart the existing food service and verify a
fresh error-free poll. Regression tests cover plan matches/deviations, corrections,
unknown numbers, day boundaries, profile isolation and incomplete journals.

Options considered: model-only prose can contradict the plan; deterministic
assessment from saved ingredients is predictable (chosen); a second model judge
adds cost and latency without improving calorie evidence. MyFitnessPal is only a
feasibility review in this scope; no credentials requested or food uploaded.

## Accepted refinements during implementation

Breakfast protein is optional; dinner eggs preferably without yolks. Explicit
whites, explicit yolks and unspecified eggs get distinct feedback. Owner's longer
term fat-loss aspiration is stored privately; calorie counting still has no
target. `/план` exposes this focus without promising its achievement.

Weekly results are requested: use the same grounded assessment for per-category
counts, repeated issues and one focus; include coverage and partial calorie
totals. `/неделя` and the existing Sunday 18:00 notice share this renderer. Do not
infer measured fat loss or a deficit. History remains in PostgreSQL and private
photo files, with corrections and independent source events preserved.
