# Calendar workflow

TL;DR: local visits remain primary. The user explicitly publishes one visit; later edits to that chosen visit synchronize automatically, with visible pending authorization/retry status. No invitation emails.

The user already approved visits, questions, Calendar and autonomous completion. This is composition of that scope, not a new approval gate. A separate local JSON outbox or extra worker service would duplicate existing persistence/scheduling: use one small profile-bound PostgreSQL publication table and the existing automation runner instead. No new daemon, framework or default external writes.

Store the opt-in, last successful content fingerprint, last attempt/result, optional validated Google link. Read a fresh immutable visit + question snapshot after local transaction commit. Serialize concurrent publications for that profile/visit without holding a database transaction across the network; use the existing private lock-file pattern. Reconcile edited content on the next attempt rather than acknowledge a newer revision accidentally. Questions only, no answer notes or lab values in Calendar. At most20 questions with explicit truncation notice when needed.

Expose configure/status/authorize/sync through CLI and publish through `/visit_calendar CODE` and the medical panel. Post-commit note/preparation/move/cancel actions attempt sync only for opted-in visits; failure never rolls back the local visit. Regular automation retries pending/changed opted-in visits. Missing authorization means locally queued, not published. Show this distinction in Russian. OAuth remains separate and interactive only at final owner acceptance.

Test with synthetic PostgreSQL and fake Calendar transport: owner isolation, duplicate publish, missing OAuth, edits after commit, cancellation, changed-during-request convergence, safe error status, no GET side effects, no full note/credential logging. No live event writes in implementation.
