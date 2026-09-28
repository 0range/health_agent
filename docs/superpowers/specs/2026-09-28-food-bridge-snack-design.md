# Small snack before a full meal

The user authorizes a personal exception to the usual interval: a small standalone
portion of nuts can bridge a long gap; a full meal follows 60–90 minutes later.
This is a scheduling preference, not a physiological claim or a new daily meal slot.

Use the existing durable food record with `category=bridge_snack`. A separate record
kind would require parallel implementations of calorie history, corrections and
delivery receipts. Leaving it as an ordinary afternoon meal would falsely close a
full meal slot. The dedicated category preserves existing storage and audit behavior.

Enable through the profile's `bridge_snack.enabled` protocol setting. Recognize
standalone text or photo captions containing 1–10 nuts, almonds or hazelnuts, and
up to 10 walnut halves (5 whole walnuts). Mixed meals, explicit meal labels,
additions, questions, future/negated consumption and unsupported portions do not
automatically qualify. Generic nuts keep their type uncertain for calorie analysis.
Do not invent non-nut alternatives from the private archive.

Store the next full meal category from the prior intake on the event date; preserve
it through a bridge snack. Explicit full-meal labels override this context. Count
snack calories, expose the separate category in day/week/shared history, and do not
count it among the four main meal slots. A full meal after a snack starts a new
record even if photographed within 40 minutes.

The first reminder is 60 minutes after the snack's stated consumption time, followed
by existing 30-minute retries (three total). Delivery eligibility ends after a
bounded 150-minute window; quiet hours, disabled reminders, snooze, skip, eaten
acknowledgement and restart durability still apply. Suppress duplicate breakfast
reminders when its timing is already owned by a morning snack. No historical meal
reclassification and no manual Telegram messages during verification.

Validate profile isolation, replay idempotency, explicit times, pending reminders,
photo grouping, nutrition failure, four-slot accounting and the actual OpenAI path
in an isolated store. Deploy settings with a private backup and compare-before-write.
