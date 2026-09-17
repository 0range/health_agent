# Food continuity and weekly focus

The food coach now separates a new meal, a revision, an acknowledgement and a
request for short help. An unlabelled food description after an open reminder
uses the reminder's meal category. Explicit additions still amend the preceding
meal. An ambiguous later message asks for confirmation rather than silently
replacing the old meal's analysis.

`уже ел` closes the relevant reminder series without creating a meal with an
invented timestamp/composition. With no open series it acknowledges without
changing the next meal. New reminders follow a subsequently recorded meal.
The existing cap of three reminders, at least 30 minutes apart, remains.

Confirmed additions retain unrelated prior foods if the model drops them.
Unsupported nutrient totals become unknown until recalculated. Explicit user
calorie estimates are stored in `user_kcal` with provenance and history; model
analysis remains separate. Day/week projections use the active user estimate.
Composition and portion changes invalidate the old override while retaining its
history. Calories remain approximate, with no automatically assigned daily limit.

## One focus

- `/фокус`: show the chosen focus or suggest one for acceptance.
- `/фокус принять`: accept a current, unexpired proposal.
- `/фокус <text>`: choose your own task, 3–240 characters.
- `/фокус итог <text>`: voluntarily record a result or obstacle, up to 400 characters.
- `/фокус стоп`: finish the current focus.

An accepted focus covers seven Moscow calendar dates, including the acceptance
date. Proposals and accepted focuses are distinct records. State survives restart
and is isolated per profile. Replaying an old acceptance returns its stored reply
without reactivating the focus or extending its dates.

The focus appears in `/план`, after breakfast feedback, and in `/неделя`. Weekly
output includes the user's supplied outcome or explicitly says it is unknown;
absence of food logs is not interpreted as failure. There are no new notification
schedules or mandatory check-ins. An existing Sunday report can propose one focus,
which still needs acceptance.

Short requests about hunger, lack of time or food choice receive a bounded tip in
the food bot and are not recorded as meals. This is intentionally a small helper,
not an autonomous multi-day coaching conversation. General health questions retain
their existing routing.

## Verification and repair

Run `uv run pytest tests/pilot tests/telegram -q`, then Ruff and mypy on changed
pilot modules. Regression scenarios cover reminder response identity, additive
corrections, acknowledgements/replay, unknown text, profile isolation, and focus
acceptance/expiry/voluntary outcomes.

Historical repairs require a private before snapshot. Superseded false meal
records retain their originals and reference the valid replacement; food-history
projections omit them. Do not publish personal conversations or repair identifiers
in the repository. This change does not alter research collection or training goals.
