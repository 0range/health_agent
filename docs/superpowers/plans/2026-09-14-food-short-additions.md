# Preserve short food additions

Problem: a short additive comment reaches the model but can disappear from the derived meal; the guard only recognizes explicit addition verbs. User requests inspection, correction and durable repair of today's record.

- [x] Extend bounded additive grammar to `ещё …`, `и ещё …`, `а ещё …`, and `+ …`, with known food terms and rejection of plans/questions/negations. Split simple lists into independent ingredients and match common diminutives without duplicating foods. Preserve prior explicit-addition/cancellation behavior.
- [x] Treat an unambiguous short grain identification (`булгур это`) as a correction to the single previously recognized grain. Preserve user text, expose correction evidence to the model, and reject stale grain output.
- [x] If model output omits a confirmed addition or contradicts a grounded correction, keep authoritative ingredients and retry once without the image, using the corrected ingredient list. If retry fails or still disagrees, retain ingredients and null nutrient estimates rather than reporting the old calories. Audit both attempts. Classify named sweets in food feedback.
- [x] Regression tests: omitted short list, de-duplication, tentative/negative/non-food phrases, repeated comments, failed retry, nutrient totals, grain correction, unchanged meal time/reminders.
- [x] Private backup and repair of affected records; rerun own evidence, inspect ingredients and estimated calories, preserve dinner and original event times. Test pilot suite and static checks; restart food bot, verify polling, commit/push code only.

Validation: 380 pilot/Telegram tests; Ruff and mypy passed. Private before/after repair evidence stays under ignored data/research/repairs. Live food polling resumed without error.
