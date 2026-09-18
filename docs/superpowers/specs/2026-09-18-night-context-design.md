# Dated sleep-location context

The user requested explicit nights away and clarified the return date. Use the
given dates, not relative wording that conflicts with the environment date.

Store one profile-scoped `shared/sleep_context` record per wake date. Keep the
night-start date, Moscow timezone, location, reason and user report/plan provenance.
Recording time is distinct from the date of the night. Do not manufacture a sleep
observation or an entire-day absence interval from a night-level report.

A free-text diary note alone would be lost in research exports. A new travel
calendar would exceed this request. Extend the existing store and export instead:
all three model contexts receive relevant dated notes; research exports retain
notes and associate WHOOP sleep records by local wake date. Away nights are marked
excluded from home-room comparison. Missing context means unknown, not home.
Planned absence is a conservative exclusion, explicitly still a plan.

Keep raw room and WHOOP measurements intact. Another participant's location must
not be inferred. Do not stop collection or automatically change nutrition/training.
No new reminder, trip questionnaire, or cross-domain coaching loop is included.

Acceptance: same dates survive reload; wrong-profile and invalid notes are absent;
future plans are distinguished from reports; exports retain hashes and raw values;
overlapping sleep records receive the correct wake-date exclusion. Personal dates,
locations and source text remain in the private database, not public documentation.
