# Telegram Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-profile Telegram Bot API connector foundation for private-chat questions, medical attachments, bot commands, and proactive messages on a local Mac.

**Architecture:** A read-only long poller receives `message` updates and advances a durable SQLite offset only after terminal handling. A strict local identity store maps one Telegram user ID to one existing Profile UUID; pure protocols isolate health-question, status/sync, and medical-inbox behavior so the connector never fabricates medical answers. The official HTTP Bot API gateway and outbound messenger own bounded downloads, safe text chunking, retry, and delivery audit.

**Tech Stack:** Python 3.13, stdlib SQLite, HTTPX, Typer, pytest.

## Global Constraints

- Use Bot API `getUpdates` long polling only; do not create a webhook or public listener.
- Accept only allowlisted users in unambiguous private chats; unknown/group/channel input receives no reply.
- Every routed request and attachment carries a Profile UUID; no cross-profile lookup or deduplication.
- Persist no incoming question, caption, reply body, attachment bytes, or bot token in audit/log output.
- Keep bot token and connector state outside Git with file mode `0600`; directories use `0700`.
- Do not call a real bot, network, cloud model, or live medical database in tests.
- Do not add an Alembic migration; connector operational state is replaceable behind protocols.

---

### Task 1: Contracts, private configuration, and durable state

**Files:**
- Create: `src/health_agent/telegram/types.py`
- Create: `src/health_agent/telegram/stores.py`
- Create: `src/health_agent/telegram/admin.py`
- Test: `tests/telegram/test_stores.py`

**Interfaces:**
- Produces immutable contexts/provenance/receipts and `HealthQuestionService`, `HealthCommandService`, `MedicalInbox`, `TelegramGateway`, and state-store protocols.
- Produces `PrivateBotTokenStore`, `SqliteTelegramState`, and UI-callable `TelegramAdminService`.

- [x] Test atomic `0600` token storage, UUID/user uniqueness, profile-separated identity lookup, durable offsets/update claims, attachment audit without content, and outbound delivery reservations.
- [x] Implement the contracts and private stores with idempotent schema initialization and no dialogue/content columns.
- [x] Run `uv run pytest tests/telegram/test_stores.py -q`.

### Task 2: Official Bot API gateway and safe outbound delivery

**Files:**
- Create: `src/health_agent/telegram/api.py`
- Create: `src/health_agent/telegram/messenger.py`
- Test: `tests/telegram/test_api.py`
- Test: `tests/telegram/test_messenger.py`

**Interfaces:**
- Consumes `TelegramGateway`, `OutboundStore`, and token value.
- Produces `TelegramBotAPI`, `TelegramMessenger`, safe API exceptions, 20 MiB bounded streaming, and <=4096-character message chunks.

- [x] Test exact `getUpdates` offset/timeout/allowed updates, webhook detection, `getFile`, streamed download without token leakage, 429 `retry_after`, non-retryable 4xx, and `sendMessage` payload.
- [x] Implement Bot API calls with sanitized exceptions and bounded retries; never expose the token-bearing endpoint.
- [x] Test deterministic text splitting, profile/chat lookup, reminder delivery keys, and repeat-send suppression.
- [x] Implement the messenger and run focused tests.

### Task 3: Private update router and long-poll lifecycle

**Files:**
- Create: `src/health_agent/telegram/service.py`
- Test: `tests/telegram/test_service.py`

**Interfaces:**
- Consumes gateway/state/messenger plus injected health-question, command, and medical-inbox services.
- Produces `TelegramUpdateService.process_update`, `TelegramLongPoller.poll_once`, and `run_forever`.

- [x] Test unknown users and non-private/channel/bot senders are silently ignored with no downstream call or reply.
- [x] Test `/help`, `/status`, `/sync`, open text questions with profile/time/message context, and no persisted text.
- [x] Test PDF/photo/voice metadata and streamed bytes, size/hash/provenance audit, same update replay, same bytes across two profiles, and offset recovery after a completed update.
- [x] Implement conservative update parsing/routing and durable terminal offset advancement; run focused tests.

### Task 4: Configuration surface, documentation, and gates

**Files:**
- Create: `src/health_agent/telegram/__init__.py`
- Modify: `src/health_agent/config.py`
- Modify: `src/health_agent/cli.py`
- Modify: `.env.example`
- Modify: `.gitignore`
- Create: `docs/integrations/telegram.md`
- Create: `docs/superpowers/reports/2026-09-04-telegram-connector-report.md`
- Test: `tests/telegram/test_admin_cli.py`

**Interfaces:**
- Produces local `telegram configure-token`, `bind`, `unbind`, and `status` administration backed by the same service a future panel can call.

- [x] Test hidden token entry, profile existence validation, bind/unbind/status truthfulness, and output free of tokens/message content.
- [x] Add settings and CLI wiring without a fake question-answer runtime.
- [x] Document official @BotFather setup, safe numeric-ID discovery/binding, long-poll limitation, file limit, exact-once caveat, and the missing real-agent/inbox runtime composition.
- [x] Run full pytest, Ruff, mypy, disposable PostgreSQL `alembic upgrade head`, `git diff --check`, and credential-pattern checks; write the factual report and commit.
