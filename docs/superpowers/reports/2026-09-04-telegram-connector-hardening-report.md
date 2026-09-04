# Telegram Connector Hardening Report

Date: 2026-09-04

Review basis: `docs/superpowers/reports/telegram-connector-review.md` at `07f552d`

## Outcome

All eleven review findings were addressed without a real Telegram call, webhook,
live health database, or fake medical-answer implementation.

1. `sendMessage` now makes one network attempt. Transport loss, 5xx, or malformed
   success is `delivery_unknown`; it is persisted and never automatically resent.
2. Retryable updates persist exact `next_retry_at`, use 5/10/20-second backoff,
   sleep until due, and become terminal `needs_attention` on attempt four.
3. Claims contain bot ID, owner, generation, attempt and lease. A heartbeat renews
   active work; renew/attachment/defer/complete use compare-and-set fencing and
   reject expired or superseded claims.
4. Outbound primary identity is `(bot_id, profile_id, delivery_key, part_index)`;
   chat and reply-text digest mismatches are explicit conflicts.
5. `configure-token` calls `getMe` before atomically publishing token+bot ID.
   Identity, offset, update and delivery state are per-bot; replacement preserves
   the old namespace and starts the new bot at an empty offset.
6. Telegram 429 becomes typed `TelegramDeferred` with the full UTC `retry_after`;
   it is not capped or synchronously slept inside HTTP handling.
7. User/profile conflict checks and binding now run in one SQLite
   `BEGIN IMMEDIATE` transaction and return the committed row.
8. Every existing private-path component and final token/SQLite target is checked
   with `lstat`; symlinks and non-regular targets are rejected before opening.
9. Attachments are downloaded to `0600` staging, bounded, hashed, fsynced, checked
   against both sizes and PDF/JPEG/PNG/OGG magic/MIME, then replayed to the inbox.
   Incorrect inbox receipts persist the connector's independently staged truth.
   Successful binary responses stay streaming and are never parsed/buffered as JSON.
10. Status distinguishes configured, remotely verified, webhook state and fresh
    poll heartbeat, and exposes `delivery_unknown_count` without message content.
11. Bot API numeric/object parsing is safe; malformed updates are quarantined,
    later valid updates continue, all poll failures are recorded, and the daemon
    backs off on permanent API/schema errors instead of terminating.

The operational SQLite initializer migrates the previous connector tables into a
lossless legacy bot-0 namespace. It is idempotent. Verified bots always use their
positive `getMe.id`, so old unverified state can never alter a new bot's offset.

## Multi-profile and privacy invariants

- One bot/user maps to one profile and one bot/profile maps to one user under DB
  uniqueness and a transactional mutation.
- The same delivery key works independently for two profiles and two bots.
- Attachment source IDs include bot ID, chat ID, message ID and file unique ID;
  the inbox still deduplicates within profile, never across profiles.
- State contains IDs, timestamps, safe codes, hashes and opaque keys only. It
  contains no incoming text/caption, reply body, filename, token, file bytes, or
  API description.
- Token, state and staging endpoints are independently Settings-overridable via
  `TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_STATE_FILE`, and
  `TELEGRAM_STAGING_ROOT`, allowing a separate live-test bot/chat environment.

## Verification

| Gate | Result |
|---|---|
| `uv run pytest -q` | PASS — 116 tests |
| Telegram focused suite | PASS — 55 tests |
| `uv run ruff check .` | PASS |
| `uv run mypy src tests` | PASS — 34 source files |
| disposable PostgreSQL migration to head inside pytest | PASS |
| legacy Telegram SQLite v1 -> bot-scoped v2, reopen/idempotency test | PASS |
| `uv run alembic heads` | PASS — single `0004_chart_integrity` head |
| `git diff --check` | PASS |
| token-pattern scan outside mocked tests/docs | PASS |

No Alembic revision was added: Telegram operational state remains a separable,
replaceable local SQLite store, while health Profile existence is checked against
PostgreSQL. The full suite's PostgreSQL is disposable; no live/local health DB was
modified.

The already-running primary local PostgreSQL was intentionally not migrated from
this worktree: a read-only container check showed revision `0005_whoop`, while this
connector branch contains migrations only through `0004_chart_integrity`; its
ordinary app connection also has no matching local `.env` credential. Applying an
older branch to that newer database would be unsafe. Integration must first rebase
this branch onto the current migration graph, then run the live migration gate.

## Live acceptance boundary

Not performed. The next live test must use a separate BotFather token, SQLite
state, staging directory and test chat through Settings overrides. It must first
verify `getMe`/webhook status and should exercise one dry-run question, attachment,
rate-limit/defer if safely reproducible, restart/offset recovery and reminder.
Production token/state are explicitly out of scope for that test.

The connector still intentionally requires injected real
`HealthQuestionService`, `HealthCommandService` and transactional `MedicalInbox`,
plus a local composition/launchd job. No medical claims are fabricated here.
