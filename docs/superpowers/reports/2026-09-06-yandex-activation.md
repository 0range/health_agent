# Yandex activation — owner profile only

TL;DR: owner-only Yandex enabled; two real questions now return cited, non-fallback answers. Telegram restarted, heartbeat ready, owner ping delivered. **1007 tests passed**. Lab archive processing is not complete: six budgeted cloud page requests added no new observations and two pages failed validation.

## Consent and deployment

- The user replied “продолжай” immediately after the explicit question asking to send necessary owner lab/WHOOP fragments to Yandex, excluding Ksyusha. Root stated this interpretation before activation.
- Ignored private `.env` selects Yandex/Qwen and allows only the existing owner profile. No secrets are in this report. Other profiles remain denied; only one live profile currently exists.
- Local extraction enabled, cloud enabled, daily page-request budget remains 20; current use 6. No retry fences reset and no failed page automatically retried.
- Telegram managed service stopped/reinstalled to load new settings. Local panel reports Telegram ready (fresh heartbeat/binding), database/Drive/Gmail/Sheets/reminders ready; sync LaunchAgent loaded. WHOOP card is configured; this does not by itself assert source freshness.
- Local `/healthcheck` HTTP200. Actual question context contains WHOOP evidence. Verified lab count is 0; 584 observations remain pending review.

## Real checks and their limits

- Initial real sleep/recovery request reached Yandex, but the application returned its insufficient-evidence fallback. `available=True` was NOT accepted as a useful answer.
- A diagnostic repeat found exact-citation violations: grouped `[WORKOUT1–WORKOUT9]` and internal `[missing_keys]`. No private answer text or patient values were logged. A static formatting-instruction prototype then produced zero invalid labels and a deterministic source footer; this is prototype evidence until the actual merged adapter passes.
- Six cloud page requests in bounded batches: completed jobs 16→20, attention 6→8, waiting-cloud 267→261; no new observations inserted. Some accepted candidates duplicate already imported rows and one diagnostic page returned no candidates. One rejected diagnostic payload had six source-evidence mismatches and one invalid field. These are extraction/layout acceptance failures, not authorization/network failures. Original evidence and pending/verified status were not rewritten.
- No lab observations were approved or made numeric from qualified values. OCR cloud prototype is still not integrated. A useful combined lab+WHOOP answer and dated blood-test charts remain separate acceptance work.

## Final code and live acceptance

- Citation feature commit `465e997`, merge `a93e6e4`. Independent task and whole-feature reviews both approved, no findings. The subagent-driven workflow kept implementation and acceptance separate; root's real-data checks caught the issue missed by the small synthetic smoke.
- Combined `uv run pytest -q`: 1007 passed in 14.31s; five inherited SWIG warnings. Ruff, mypy (110 source files), lock and diff checks clean.
- Actual merged application, real owner data: sleep/recovery question returned a 2875-character answer including deterministic sources in 2.71s; overview question returned 2322 characters including sources in 1.88s. Both had no safe error and were not the insufficient-evidence fallback. These tests establish transport/citation acceptance, not comprehensive clinical quality or complete lab coverage.
- Telegram restarted again after merge. Owner-only readiness message sent once through the existing profile-bound/idempotent messenger; API acknowledged one message. Local heartbeat card ready afterward.
- A user-originated Telegram question is still needed to demonstrate real receive→answer→delivery; a CLI/model call or outgoing ping alone does not prove that loop.

Remaining product work: robust extraction/layout handling and review of real lab data, useful dated lab charts and combined insights; doctor/calendar and recurring-checkup scenarios remain outside this completed provider activation. No claim that v0.1 is finished.
