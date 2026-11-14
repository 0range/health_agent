# Historical registration windows and Sunday digest

The user requested a persistent 2025/2026 comparison, estimated registration
dates for season 2027, a weekly table, and countdown/status labels. They chose
Sunday at 13:00 Europe/Moscow. The existing daily cloud task remains the owner;
Sunday's table replaces that day's ordinary short status. Newly opened entries
take priority and include the direct registration link. Training bot remains
the destination. Security monitoring is outside this change.

Store the evidence in `docs/research/race-registration-history-2027.md` and copy
the operational baseline into the cloud task, which cannot rely on local files.
Preserve original route credentials privately. A prior-year opening in the
preceding calendar year must retain that offset when projected to season 2027.
Use 2026 as the anchor and the two observed seasons as a descriptive range,
never a promised opening date or a statistical confidence interval. Keep
festival-only announcements separate from confirmed individual 1/8 openings.

Weekly statuses distinguish officially open, announced opening, estimated date
within seven days, estimated date passed, and no confirmed date. An estimate
passing is not evidence that registration has opened or that places are gone.
Unavailable sources are reported explicitly. Sunday reports retain historical
dates and show the live check date. New official information overrides forecasts
without rewriting historical observations.

Record sellout evidence with its precision: explicit duration, interval between
announcements, or unknown. Do not equate a sold-out announcement timestamp with
the moment the last slot was purchased. White Nights 10 km has a documented
under-one-day sellout in both seasons. Checking once daily can miss that window.
An optional hourly check in expected windows and five-minute check around an
announced opening was offered separately; do not claim it active before its
schedule and delivery have been verified.

The user subsequently approved both frequencies and required a direct
registration link. Implement a separate small Railway service from this repo,
reusing the existing public-source adapters. Keep the daily ChatGPT task for
the digest and historical table. The worker owns immediate opening alerts;
its authenticated status endpoint exposes delivery receipts to avoid duplicate
announcements in the daily digest. It receives only the training relay key.

Use hourly checks inside historical ranges expanded by seven days before and
fourteen days after. Outside those ranges check daily. An explicit official
opening enables five-minute checks from 30 minutes before until 24 hours after.
Read official event pages/channels for explicit dated announcements hourly;
also accept exact announced openings from the daily task through an authenticated
endpoint. Never infer an exact opening time from a historical date. Persist
observations, announcements and an immutable alert outbox on a Railway volume;
the existing relay supplies durable idempotency. A restart must not duplicate
an alert or lose a pending one. All alerts carry the distance's registration URL.

No automatic participant registration or payment is authorized. The user asked
about that capability; they have not chosen a race or a price limit.

Verification: read back the saved task, confirm enabled state, Sunday rule,
unchanged intended daily 13:00 Moscow execution times and training-only routing.
Check forecast year offsets and compare source links. No local scheduler and no
duplicate weekly cloud task are needed for this change.
