# Food reminder repeats

User-authorized change: at most three delivered notices per pending meal, 30 minutes apart, including breakfast. First notice keeps the existing meal target; repeats use actual delivery times so restart cannot release a burst. Logging a new meal, skipping, pausing or quiet hours suppresses the old series. Snoozing changes the next eligible time but does not reset the three-notice cap. Preserve existing first-notice keys and count legacy receipts; attempted delivery is not a receipt. Repeated notices identify the intended next meal and ask for a log rather than asserting that the user has not eaten.

- [x] Add a small receipt-based repeat selector in `pilot/food_reminders.py`: legacy key for first notice, `:repeat:2/3` thereafter; actual-delivery spacing; first-notice expiry and bounded repeat grace.
- [x] Integrate in `FoodCoach` meal/breakfast due logic. Apply breakfast skip/snooze to its date-scoped series. Keep meal category boundaries, time corrections, profile isolation and quiet hours.
- [x] Tests in `tests/pilot/test_food_reminder_repeats.py`: three deliveries with polling jitter; no fourth; failed sends; restart spacing; new meals/skip/pause; quiet hours; snooze and cap; breakfast; legacy receipts.
- [x] Run pilot/Telegram regression suites, Ruff and mypy; update runbook/help, restart food bot, verify fresh poll and current due state, commit and push code/docs only.

Validation: 357 pilot/Telegram tests passed; focused Ruff and mypy passed. Live due inspection at rollout found no stale notice eligible. Food bot rollout and remote commit are verified as the final steps.
