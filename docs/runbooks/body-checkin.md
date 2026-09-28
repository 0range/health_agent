# Manual scale measurements

Main Health Agent supports a deterministic three-question flow: weight in kg,
body fat in %, and muscle mass in kg or %. No LLM call is made for this flow.

- `/замер` or **Начать замер**: today's date in Europe/Moscow, stated in prompts.
- `/замер YYYY-MM-DD` or `/замер DD.MM.YYYY`: another measurement date.
- `/замер вчера` is also accepted; future and invalid dates are rejected.
- `вес 75.1 кг, жир 21.3%, мышцы 55.4 кг`: all values in one message.
- `75.1 кг вес, 21,3% жир, 55.4 масса мышц`: values before labels work too.
- Skip unknown answers; missing values stay null. Unitless muscle answers mean
  kg, as explicitly asked in the question; use `%` for a displayed percentage.
- Review all three values and date, then press Save/Edit/Cancel. No empty
  observation is saved. `/замер отмена` cancels a draft.
- `/замер напоминания вкл` enables Thursday/Sunday 08:00 Moscow prompts;
  `/замер напоминания выкл` disables them. Manual entry remains available.

Only main bot routes this flow. Existing `/вес`, food and sleep commands retain
their behavior. Unknown conversational messages pass through even during a draft.
Voice transcription still follows the existing coach route; these commands and
numbers are currently intended as text messages, not guaranteed voice ingestion.

Each draft and reply is scoped by profile. Confirmation buttons carry a draft
number. Incomplete drafts expire after 24 hours. Replayed updates reuse the
original reply, including a crash between an answer patch and reply persistence.
One confirmed check-in creates `shared/body_measurement`; known weight is also
projected to `shared/weight` with the same source key. Both inserts are idempotent
and a retry completes a partial projection. A newly started and confirmed check-in
is a new observation, even on the same day. The report uses distinct measurement
days, so repeated weighing cannot increase the weekly sample count.

Measurement date and recorded_at are separate. `at` is local midnight for daily
alignment with timestamp_precision=day; the actual weighing hour is unknown.
Today's last measurement appears immediately in `/инсайты`, while completed-week
comparisons exclude today. Body fat and muscle mass are scale estimates, not
direct measurements of tissue change. kg and % series remain separate.

Reminders use existing main-bot dispatch and delivery receipts, once per eligible
date, within 08:00–12:00 Moscow. They are omitted after a saved measurement that
day. If the Mac/service is unavailable throughout that window, no stale reminder
is sent later. A reminder opens the questionnaire via a button; it doesn't create
an unattended draft or reinterpret sleep check-in answers as measurements.

Private proof and any actual user measurements belong under ignored `data/`.
Never commit live health observations or test fixtures derived from private data.
