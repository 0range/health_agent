# Gmail medical connector report

## Result

Mocked-ready, read-only Gmail ingestion foundation for multiple accounts per
health profile. The first scan uses a configurable seven-day lookback; later
scans use a durable Gmail `historyId`, with automatic lookback recovery when the
cursor has expired. Likely medical PDF/images are streamed into an injected
importer with Gmail provenance, SHA-256, and size. Ambiguous items remain
internal and do not interrupt the user.

No database migration was added. Profile/account-scoped store and importer
protocols keep this connector separate from the concurrent chart/provenance
migration; the handoff to the existing medical importer is documented in
`docs/integrations/gmail.md`.

## Verification

- Gmail connector: 37 mocked tests passed.
- Full repository: 98 tests passed.
- Ruff and mypy passed.
- Fresh `alembic upgrade head` reached `0004_chart_integrity (head)` in a
  disposable PostgreSQL database.
- CLI help for `gmail`, `configure`, and `sync` rendered successfully.
- No real mailbox, credential, or user medical file was used.

Known unrelated migration concern: `alembic downgrade base` fails inside the
pre-existing `0003_review_corrections` downgrade because the
`verified_lab_history` view still depends on the column it tries to remove.
This branch does not modify that concurrent migration chain; fresh upgrades are
unaffected.

## External activation step

Create/download one Google OAuth **Desktop app** client with Gmail API enabled,
save its JSON at the ignored configured path, and run `gmail auth` once for each
configured account slot. The connector requests only `gmail.readonly`.

Implementation behavior follows Google's official Gmail Python quickstart,
OAuth scope reference, message/attachment resources, synchronization guide, and
error-handling guide linked in `docs/integrations/gmail.md`.

## Review fix round (`gmail-connector-review.md`)

The initial report above is retained as historical evidence for `e084768`. The
review blockers are closed in the follow-up implementation:

- production CLI routes validated PDFs through the common profile-aware
  PostgreSQL `import_document` and lab review pipeline; status distinguishes
  staged, medically imported, duplicate, OCR, and attention outcomes;
- body-only appointments and metadata-ambiguous PDFs receive bounded local
  content classification, with a safe internal attention listing;
- current Gmail labels make full and incremental Spam/Trash policy consistent,
  including mail restored through label transitions;
- verified identity and credentials publish together only after mailbox checks,
  wrong-account/failed reauthorization preserves the old token, and the same
  mailbox cannot cross health profiles;
- a cross-process account lock serializes state/cursor commits, revisions retain
  full occurrence provenance, and delivery is documented/tested as at-least-once;
- attachment size and signature validation precede importer effects, while API
  and OAuth callback timeouts, transport retry, repeated-page guards, and
  symlink rejection are explicit;
- OAuth status, sync freshness, and safe errors are durable; External/Testing's
  seven-day refresh-token expiry is no longer described as unattended setup;
- the pre-existing 0003 view dependency is fixed and covered by fresh
  up/down/up migration testing.

Final verification: 57 Gmail tests and 119 repository tests passed, including a
fresh disposable PostgreSQL upgrade/downgrade/upgrade regression. Ruff and mypy
also passed. No live mailbox, credential, or user medical file was used.
