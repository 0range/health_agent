# WHOOP Hardening Round 3 Plan

**Goal:** Preserve token/database consistency when authorization fails after the
new `token_generation` has already committed.

**Design:** A coordinated replacement never guesses rollback on exceptional exit.
While the outer account-operation lock remains held, authorization re-reads the
committed generation and resolves the durable journal to candidate or previous.
Finalization uses that generation, is idempotent, and may safely be retried after a
restart.

- [x] Add failing tests for DB-committed cleanup failure and restart recovery.
- [x] Leave coordinated journal unresolved on exceptional context exit.
- [x] Make replacement finalization generation-aware and idempotent.
- [x] Reconcile every authorization exception after releasing the inner token lock.
- [x] Inject failures after DB commit through journal fsync/cleanup; preserve the
  existing DB-commit-failure rollback test.
- [x] Run focused and full pytest, Ruff, mypy, migration, and diff gates.
- [x] Commit without live credentials or push and append the SDD report.
