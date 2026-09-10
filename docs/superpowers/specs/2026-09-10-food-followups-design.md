# Breakfast reminders and meal additions

The owner reported a missing breakfast prompt and bread absent from the lunch
analysis, and requested evidence of live sensor/WHOOP ingestion. This is an
authorized correction of the existing food capture/reminder workflow.

Observed causes: FoodCoach has only relative post-meal and weekly reminders;
there is no breakfast notice. The lunch comment was durably bound to the right
meal, but model output repeated the old plate and the deterministic renderer never
acknowledged the planned addition. Some text-meal responses also use a scalar
`foods`/`unknowns` string, currently discarded by normalization.

Implement breakfast at 09:00 Moscow by default, configurable with `/завтрак HH:MM`
and `/завтрак выкл|вкл`. Deliver once per date within two hours, respecting global
pause/quiet hours and already logged breakfast or a later meal. Delivery receipts,
not attempted sends, suppress retry. Do not send today's missed prompt in the
afternoon. Keep the existing post-meal interval.

Treat clear short addition statements as durable evidence, separate planned from
confirmed additions and acknowledge both in replies. Confirmed additions must
survive a model omission; invalidate nutrient totals if the model missed an item,
then request a complete corrected meal on the next analysis. Planned additions
must not be silently counted as consumed. Preserve original captions/comments,
model revisions and meal timestamps. Scalar food/unknown descriptions become
single-element lists, preserving compound dish names without arbitrary splitting.

Repair the actual lunch using the owner's new confirmation that bread was added;
retain the earlier future-tense message as originally written. Reparse affected
recent scalar food outputs from their original model JSON. No fabricated messages
or synthetic live meals, and no unsolicited Telegram replies.

For ingestion evidence, query original rows/series, export private CSV and report,
compare a bounded native source window to saved values, and show a before/after
snapshot from the independently running collectors. Office/bedroom provenance
remains explicit: the latest owner message establishes the monitor is now in their
shared bedroom, but exact move time remains unknown; do not backdate room changes.
