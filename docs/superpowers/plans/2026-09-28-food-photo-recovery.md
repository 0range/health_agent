# Food photo recovery implementation plan

**Goal:** Recover the user's apple photo and keep useful calorie estimates when another photo or a transient provider error occurs.

**Diagnosis:** The failed production request retained only `ValueError`, so its original upstream cause cannot be established. An exact replay completed and estimated the full lunch including an apple at 704 kcal. Independently reproduced code defect: the second photo adds an observation and the same derived result to aggregation; `len(values) > 2` then unconditionally discards all nutrient totals, even when the new response covers the whole meal.

**Design:** Keep immutable photos, one meal and the current model/provider. Ask explicitly for a whole-meal estimate, preserve totals only when the returned foods cover the prior ingredients after corrections, and never sum repeated views. Keep the last successful estimate separate from the current result on failure and label it as previous in replies. Retry one explicitly transient OpenAI failure and retain safe diagnostic codes without raw vendor messages. Do not resend Telegram messages manually.

## Steps

- [x] Add regression tests for a 600 kcal plate plus an apple yielding a 704 kcal whole-meal estimate; repeated view stays 704; an apple-only estimate cannot replace the meal total.
- [x] Test provider/invalid-JSON failure after a good estimate and successful reprocessing with the same meal/photo identity.
- [x] Implement whole-meal scope and ingredient-coverage validation in `pilot/food.py`; keep immutable photo observations and last successful estimate. Update incomplete feedback without misrepresenting an old estimate as a current total.
- [x] Add allowlisted error metadata and a single bounded retry for connection/timeout/408/409/429/5xx errors; never retry billing, authorization, invalid input or refusals. Test fake SDK outcomes and durable model-run metadata.
- [x] Run pilot, AI and question/lab boundary regressions, Ruff and mypy on touched adapter code. Verify an isolated replay of the actual apple photo and original meal data.
- [ ] Back up the affected private records, deploy/restart after checking no update is in flight, and reprocess the original meal without creating another meal or changing timestamps. Verify day totals and save an audit record.
- [ ] Commit/push main, verify running service and remote HEAD, then report the cause, recovered calories and approximate nature of the estimate.
