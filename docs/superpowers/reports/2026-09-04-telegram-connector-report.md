# Telegram Connector Implementation Report

> Superseded by the post-review hardening report:
> [`2026-09-04-telegram-connector-hardening-report.md`](2026-09-04-telegram-connector-hardening-report.md).

Date: 2026-09-04

Branch: `codex/v1-telegram`

Base: `305b837`

## Outcome

Implemented the local, multi-profile Telegram connector foundation without a
webhook, fake medical answers, or a real-network test dependency.

- Official Bot API gateway: `getUpdates`, `getWebhookInfo`, `getFile`, streamed
  download and `sendMessage`.
- Durable SQLite offset/update/attachment/outbound audit, containing no dialogue
  body or attachment bytes.
- Strict one-to-one allowlist from Telegram user ID to an existing Profile UUID;
  only the bound private chat is accepted.
- UI-callable protocols for questions, `/status`, `/sync`, and medical inbox;
  every call carries Profile UUID and message/time context.
- Document/photo/voice provenance, independent streaming SHA-256 and 20 MiB
  bound; inbox deduplication contract is `(profile_id, source_external_id)`.
- Safe 4096-character output chunking and duplicate reply/reminder suppression.
  The hardening round changed mutating transport/5xx handling to explicit
  `delivery_unknown` without automatic retry.
- Service-friendly token/bind/unbind/status administration plus safe numeric ID
  discovery. Token and operational state default to ignored local `0600` files.
- No Alembic revision was added, avoiding conflicts with other connector branches;
  replaceable Telegram operational state is isolated behind protocols.

## Verification

All test I/O to Telegram and connector downstream services was mocked. No bot,
model, cloud service, or live medical database was contacted.

| Gate | Result |
|---|---|
| `uv run pytest -q` | PASS — 88 tests |
| `uv run ruff check .` | PASS |
| `uv run mypy src tests` | PASS — 33 source files |
| disposable PostgreSQL + `alembic upgrade head` inside pytest | PASS — head `0004_chart_integrity` |
| `uv run alembic heads` | PASS — one head, `0004_chart_integrity` |
| `git diff --check` | PASS |
| credential-pattern scan outside mocked tests/docs | PASS — no candidate token |

The migration gate is intentionally the repository's disposable PostgreSQL
fixture, not the user's local/live database. This branch changes no medical
schema.

## Official Telegram behavior used

- [`getUpdates`](https://core.telegram.org/bots/api#getupdates): long polling,
  webhook mutual exclusion, offset confirmation, `allowed_updates`, 24-hour
  update retention.
- [`getFile`](https://core.telegram.org/bots/api#getfile): cloud download path and
  20 MiB download limit.
- [`sendMessage`](https://core.telegram.org/bots/api#sendmessage): 1–4096 character
  text limit.
- [`ResponseParameters`](https://core.telegram.org/bots/api#responseparameters):
  429 `retry_after` handling.
- [Bot Features](https://core.telegram.org/bots/features): @BotFather creation and
  the token-as-password security model.

## Required live owner step

Not performed: no real bot/token/account was supplied, and tests must not use
one. The owner must create the bot with @BotFather, run the hidden token prompt,
send `/start`, run `telegram discover-id`, and bind that numeric ID to the desired
Profile UUID. Exact commands and limitations are in
[`docs/integrations/telegram.md`](../../integrations/telegram.md).

## Known boundary / next integration

This is a connector foundation, not a fabricated agent runtime. A composition
root still has to inject the actual `HealthQuestionService`,
`HealthCommandService`, and `MedicalInbox`, then run the poller as a local Mac
job. The inbox performs PDF import/OCR/voice handling; Telegram only validates
and transports the profile-scoped stream.

Telegram `sendMessage` has no application idempotency key. Local delivery keys
prevent normal replays, while an unavoidable crash between remote acceptance and
local acknowledgement is handled conservatively as at-most-once to avoid sending
duplicate medical replies. The operational audit exposes this state without
storing message content.
