# Three-field scale check-in

The user replaced screenshot ingestion with a short manual questionnaire. Main
Health Agent asks for weight (kg), body fat (%), and muscle mass (kg or %).
Unknown values can be skipped. No image recognition or new provider is needed.

Use a deterministic parser and a durable draft in shared/body_checkin. `/замер`
starts a measurement for the current Moscow calendar date, visibly stated in every
prompt. `/замер YYYY-MM-DD` starts a dated historical measurement. A labelled
single message can supply all three numbers, with decimal commas supported.
Muscle percentage and mass stay distinct; missing values never become zero.
After the third answer, show the date and values, then Save/Edit/Cancel buttons.
Edit restarts the draft before saving; after saving a new check-in is a separate
observation. Do not silently replace a confirmed historical observation.

Save all three fields in shared/body_measurement and project known weight into
shared/weight with a common stable source key. Store the reported date separately
from recorded_at, mark precision=day, and use local midnight only as a storage
anchor. Never present that anchor as a known weighing time. Both inserts are
idempotent, including a retry after a partial save. Scope drafts, replies and
measurements by profile; preserve the configured main-bot profile guard.

Only an explicit measurement command or recognized answer consumes a message.
Sleep questions, diary buttons and other existing commands keep their routes.
Drafts expire after 24 hours. Replayed Telegram updates return their original
reply, and Save buttons contain a draft token so a stale button cannot save a
new draft. Original numerical inputs stay in the existing input journal.

Joint observations include daily medians of body fat and separate muscle units.
For weight and composition, compare weekly means only with at least two distinct
measurement days in each week and describe the difference as preliminary, not a
fat-loss result. Duplicate observations within a day cannot create extra days.
Expose body-composition estimates to the grounded main-bot context. Do not infer
body fat or muscle mass from weight, food or WHOOP snapshots.

The user explicitly chose Thursday/Sunday 08:00 Moscow reminders. Enable them
for the owner's profile only, through shared/settings/body-checkin. Existing
outbound delivery persists one reminder receipt per date. The morning catch-up
window ends at noon; a saved measurement for that day suppresses the reminder.
Both reminder toggles are available as `/замер напоминания вкл|выкл`.
Actual values supplied in the conversation authorize the current test; synthetic
fixtures may exercise tests but cannot enter production data.
