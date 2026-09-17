# Personal meal assessments (17 September 2026)

Owner protocol `meal_assessment_version=1` enables a deterministic short Russian
assessment with emoji after a meal, photo, portion change or comment. Legacy
profiles keep their own rules. Ingredients and confirmed additions ground the
assessment; arbitrary model prose does not become dietary advice.

The owner's current plan:

- Breakfast: slow carbohydrate source and fruit; protein is optional.
- Lunch: Harvard plate components and fruit.
- Afternoon: Harvard plate components and a permitted treat.
- Dinner: protein, optional vegetables; eggs preferably without yolks.

The renderer cannot establish plate proportions from ingredient presence. It
also distinguishes named whites, explicit yolks and eggs with unknown yolk use.
Absence of a food from the record is not proof that it was not eaten. A treat is
not mandatory and never triggers a suggestion to eat more purely to complete it.

Calories are estimates; there is no daily target. Daily totals use current meal
versions, not photo observations/revisions. Unknown numbers remain unknown,
partial totals are marked, and four-meal coverage counts distinct meal categories.
The Moscow meal date scopes the total, including corrections of older entries.
Photos are private files; PostgreSQL retains metadata, source text, comments,
analysis and revisions. Nothing in this change removes historical records.

Commands: `/план` gives plan and saved longer-term focus; `/сегодня` gives daily
calories by meal. Supper feedback includes that daily breakdown. `/неделя` gives
last-seven-date coverage, recorded calories, composition results and recurring
issues, plus one action for next week. The existing Sunday 18:00 Moscow notice
uses the same report, subject to reminder settings/quiet hours and delivery
receipts. Sunday is a partial day at delivery; the report explicitly describes
logged food, not complete dietary intake. No new daily push was added.

Desired focus: a user-stated fat-loss target, stored privately as an aspiration rather than a
validated forecast. Food records alone cannot prove a deficit or fat loss; no
automatic deficit target is prescribed. Baseline weight and weekly trend would
be required before assessing progress toward this goal.

Validation: 397 pilot/Telegram tests, Ruff, mypy; live protocol backup and local
rendering of stored records; service restart and fresh Telegram poll. No test
Telegram messages and no reanalysis or mutation of historical meals.
