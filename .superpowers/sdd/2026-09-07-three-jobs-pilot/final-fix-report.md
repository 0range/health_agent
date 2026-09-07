# Final fix report

## Changes

- Restricted pilot text and non-medical attachment/voice processing to the configured activation profile before durable pilot input, Brain, or COROS-bound coach invocation. Existing sleep-bot medical document routing and legacy text handlers remain available.
- Added regression coverage with two identities bound to one bot, proving the second profile cannot invoke the pilot coach/provider or create pilot-domain records.
- Broadened training model invocation fallbacks to the complete exception boundary while leaving all storage operations outside the catches. Added real OpenAI SDK timeout and rate-limit exception coverage for proposal, dialogue, and cached scheduled reflection.
- Added bounded recent training dialogue to weekly planning, plus the latest proposal and accepted plan to dialogue. Explicit `/сохранить план` acceptance remains unchanged.
- Rejected food snoozes whose target exceeds the meal reminder lifetime or lands in quiet hours, without writing a control or promising delivery.
- Reused COROS sync normalization for interactive activity ingestion so date-only rows use Moscow midnight and `timestamp_precision: day`.
- Added `/порция 200 г` to food help and removed the unverified voice invitation from sleep help.

## Verification

Command:

` .venv/bin/pytest -q tests/pilot/test_training.py tests/pilot/test_food.py tests/pilot/test_runtime.py tests/pilot/test_coros_sync.py `

Output: `46 passed, 5 warnings in 1.85s` (warnings are existing SWIG deprecations).

Command:

` .venv/bin/pytest -q tests/pilot `

Output: `86 passed, 5 warnings in 2.34s` (warnings are existing SWIG deprecations).

Command:

` .venv/bin/ruff check src/health_agent/pilot/runtime.py src/health_agent/pilot/training.py src/health_agent/pilot/food.py src/health_agent/pilot/coros_sync.py tests/pilot/test_runtime.py tests/pilot/test_training.py tests/pilot/test_food.py `

Output: `All checks passed!`

Command:

` .venv/bin/mypy src/health_agent/pilot/runtime.py src/health_agent/pilot/training.py src/health_agent/pilot/food.py src/health_agent/pilot/coros_sync.py `

Output: `Success: no issues found in 4 source files`

Command: `git diff --check`

Output: clean (no output).

## Self-review and concerns

- Confirmed profile rejection occurs before pilot persistence and coach/Brain invocation. Rejected attachments are consumed only to return a receipt matching the Telegram staging checksum; they are not copied into the pilot vault. Sleep medical documents deliberately retain the established legacy route.
- Confirmed broad catches wrap only Brain calls; proposal/reply/reflection persistence remains outside and therefore observable on storage failure.
- Training history is profile-scoped by the Store API and bounded to 12 conversation records; plan context is limited to one latest proposal and one latest accepted plan.
- Snooze validation uses the same 4.5-hour expiry and configured quiet-hour calculation as delivery.
- No credentials, private data, or real provider calls were read or executed. No architecture expansion or medical content was added.
- `ruff format --check` reports pre-existing formatting drift in several touched legacy files; applying it would create a large unrelated mechanical diff, so the scoped `ruff check` gate was used and passed.
- The deferred Apple counter terminology observation remains unchanged, as authorized.
