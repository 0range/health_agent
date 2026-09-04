# Health Question Loop Report

## Delivered

Commit `5f1bbe5419b18910aa5b873b9f592adcd46e1f04`
(`test: cover bound health question telegram loop`) completes Task 4 of the
approved Health Question Loop plan.

- Added a true offline component/integration test for one bound, free-form
  Telegram update. It uses the existing `TelegramLongPoller`,
  `TelegramUpdateService`, `TelegramMessenger`, and real `SqliteTelegramState`.
- The test seeds a disposable PostgreSQL database with a verified lab and
  normalized WHOOP sleep fixture for the bound profile, plus different sentinel
  evidence for another profile. It uses the real profile-scoped context builder
  and a mocked OpenAI Responses client; the deterministic reply has `[LAB1]`,
  `[SLEEP1]`, and the application-owned `Sources:` footer.
- The test asserts that another profile’s sentinel metric/value is absent from
  both the mocked Responses request and the delivered reply, and verifies the
  durable update and outbound-delivery audit rows.
- Added [health-questions.md](../../health-questions.md) and concise README
  commands, privacy boundaries, and live-only validation requirements.
- Fixed typing in the injected question-application seam and existing test
  assertions so the full static check includes tests. Alembic migration scripts
  are deliberately excluded from mypy because they are dynamically executed
  migration declarations; Alembic itself checks their metadata below.

## Gates run

All commands below ran locally on September 4, 2026 with synthetic fixtures;
none used a real OpenAI key, Telegram bot, OAuth authorization, personal health
record, or network request to those services.

| Gate | Result |
| --- | --- |
| `uv run pytest -q tests/questions/test_question_loop_integration.py` | PASS — 1 test |
| `uv run pytest -q` | PASS — 393 tests (5 existing PyMuPDF/SWIG deprecation warnings) |
| `uv run ruff check .` | PASS |
| `uv run mypy .` | PASS — 102 source files, including tests |
| Alembic metadata check on the established disposable PostgreSQL fixture (`command.check`) | PASS — `tests/whoop/test_schema.py::test_whoop_migration_matches_sqlalchemy_metadata` |

`uv run alembic check` without an explicit disposable `DATABASE_URL` would use
the local runtime database configuration. To avoid touching a potentially live
database, this task used the repository’s established Docker-backed disposable
PostgreSQL fixture, which migrates an isolated `test_health_agent_<uuid>`
database and invokes the same Alembic `command.check` API. The full suite also
executes that check.

## Responses API alignment

The adapter uses a single stateless `responses.create` call with `store=False`,
bounded output, and a one-way safety identifier. It reads `output_text` only
after a completed result and avoids conversations and response chaining. The
implementation and mocked call contract are aligned with OpenAI’s official
[Responses create reference](https://platform.openai.com/docs/api-reference/responses/create).
The isolation, constrained input, safe failure handling, and test-first
validation are consistent with OpenAI’s [API safety best practices](https://developers.openai.com/api/docs/guides/safety-best-practices).

## Remaining live-only validation

No live Telegram or OpenAI validation is claimed. Before relying on the loop,
the owner must separately validate a real BotFather credential and bound private
chat delivery, a permitted OpenAI key/account using non-sensitive test data, and
the owner’s intended data-processing/privacy posture. The local runtime must
also have its PostgreSQL schema applied and appropriate verified evidence before
an answer can be useful.

## Whole-branch review fixes — September 5, 2026

Implementation commit: `d672a4f7d14a60abb3c1909fbc6ac081d311aa7f`
(`fix: preserve question replies and enforce safety context bounds`).
This follow-up addresses R1–R4 in
[the whole-branch review](health-question-loop-review.md); it is implementation
and verification evidence, not an independent re-review verdict.

- R1: A private reply-only spool publishes the final rendered answer before
  Telegram delivery. Opaque bot/update filenames and a hashed authenticated
  profile/user/chat scope prevent cross-binding reuse. Regular `0600` files in
  a `0700` directory are capped at 128 KiB; publication is atomic and cannot
  overwrite an existing reply. No question, raw context, credentials, or
  conversation history is stored. The final answer includes its Sources footer
  and therefore contains the medical facts already prepared for delivery.
  First-part and later-part deferrals reuse those bytes across process restarts.
  Existing part hashes, conflicts, and unknown-send fencing remain unchanged.
  An optional cleanup hook runs only after a committed terminal update; startup
  and incoming questions sweep orphan files older than seven days. Expired or
  manually removed spools cannot guarantee replay and the outbound checks remain
  fail-closed. PDF imported/duplicate results now share canonical receipt status
  and reply text, avoiding both attachment-audit and outbound conflicts.
- R2: Direct emergency statements take precedence over informational question
  handling; the exemption is narrowed to informational constructions. Bilingual
  safety and service tests include implicit Russian first person, contractions,
  and third-person emergencies, and prove that retrieval/responders are skipped.
  Generic chest-pain and difficulty-breathing questions stay informational.
- R3: Generic analyte trends no longer imply weight. Current weight has its own
  intent. A typed limitation distinguishes a prohibited weight inference from
  an entirely unanswerable request. Weight-only change stays local; mixed
  sleep/weight-change requests can answer sleep while retaining the prohibition.
- R4: Body sync-as-of snapshots obey both inclusive temporal bounds. Exact UTC
  selection, calendar-day laboratory resolution, per-source cap, and observation
  versus synchronization semantics are included in structured model input and
  the deterministic Sources footer. Stale/future and boundary regressions pass.
- Ancillary: The default output cap is 2,000 tokens (configurable 64–8,000) with
  explicit low reasoning effort; the SDK timeout is 30 seconds with automatic
  retries disabled. The completed-status guard remains in place. The one-way
  safety identifier is now 64 characters, within the current documented limit.

### Official SDK/API evidence and limits

Installed OpenAI Python SDK `3.8.0` was inspected locally. Its Responses `create`
signature accepts `extra_headers`, `reasoning`, and `timeout`; it has no dedicated
idempotency argument, and the base client initializes `_idempotency_header` to
`None`. Official documentation establishes `X-Client-Request-Id` as a tracing
header for Responses, not a deterministic response replay contract. The hashed
Telegram request ID is propagated through the application/responder using that
supported header; delivery determinism comes from the local spool. No unsupported
`Idempotency-Key`, prompt-cache, or sampling-determinism claim was introduced.
See [official request-ID documentation](https://developers.openai.com/api/reference/overview#supplying-your-own-request-id-with-x-client-request-id)
and the [Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create).

The output cap includes reasoning tokens. Larger defaults and explicit effort
address the static budget concern but do not establish useful live completion
rates or clinical correctness. See [reasoning-token guidance](https://developers.openai.com/api/docs/guides/reasoning#controlling-costs).

### Gates rerun

| Gate | Result |
| --- | --- |
| `uv run --offline pytest -q tests/questions tests/telegram/test_service.py` | PASS — 123 tests before the last two adapter regressions were added |
| `uv run --offline pytest -q` | PASS — 427 tests, including both final adapter regressions; 5 existing PyMuPDF/SWIG deprecation warnings |
| `uv run --offline ruff check .` | PASS |
| `uv run --offline mypy .` | PASS — 104 source files |
| Disposable PostgreSQL Alembic `command.check` | PASS within the full suite — `tests/whoop/test_schema.py::test_whoop_migration_matches_sqlalchemy_metadata` |
| `git diff --check` | PASS |

The new delivery tests use real SQLite/update/messenger components and synthetic
PostgreSQL retrieval. A stochastic fake responder and changed database make a
second generation observably different; the restart test confirms no second
generation occurs and the exact original multipart bytes complete. A PDF fake
importer returns imported then duplicate under a first-send deferral, and both
attachment/outbound audits complete. Spool tests cover restart, authenticated
scope conflicts, permissions, symlinks, oversized reads/writes, and orphan TTL.

All test API transports were mocked; no real credentials, health records,
Telegram/OpenAI/OAuth service requests, or runtime database were used. Tests ran
with offline dependency resolution and an already-cached PostgreSQL Docker image;
the fixture uses local Docker/TCP. Official documentation retrieval was read-only.
Live BotFather/account/model validation and synthetic clinical/adversarial
evaluation remain separate owner-operated steps.

## Final approval gate rerun — September 5, 2026

After `283618f` closed the final PDF-receipt and concurrent-spool-sweep findings,
and independent review commit `cdec3da` recorded **SPEC PASS / QUALITY APPROVED /
OVERALL READY**, the complete local gates were rerun from the clean branch:

| Gate | Result |
| --- | --- |
| `uv run pytest -q` | PASS — 430 tests; 5 known PyMuPDF/SWIG deprecation warnings |
| `uv run ruff check .` | PASS |
| `uv run mypy .` | PASS — 104 source files, including tests |
| `uv run pytest tests/test_schema.py -q` | PASS — 15 tests, including disposable-PostgreSQL Alembic metadata checks |
| `git diff --check` | PASS |

These final gates used only synthetic fixtures and mocked external transports.
They do not change the live-only limitations above.

## Final integration rebase — September 5, 2026

The complete question-loop branch was rebased from its original `1387d5e` base
onto exact main commit `1f53d32` (`codex/v1-slice-1`). The rebased code tip before
this integration note was `e5a2228`. Conflicts were limited to the shared CLI and
README surfaces and were resolved as a union: management panel, Drive, Gmail,
WHOOP, staging, macOS automation, and all existing commands remain present while
`question ask/status` and `telegram run` remain registered. The OpenAI dependency,
configuration fields, and lock entries are retained.

Post-rebase verification from the clean question worktree:

| Gate | Result |
| --- | --- |
| `uv run pytest -q` | PASS — 570 tests; 5 known PyMuPDF/SWIG deprecation warnings |
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 69 source files |
| `uv lock --check` | PASS — 71 packages resolved without changing the lock |
| `git diff --check` | PASS |
| `uv run pytest tests/whoop/test_schema.py::test_whoop_migration_matches_sqlalchemy_metadata -q` | PASS — disposable PostgreSQL/Alembic metadata check |

`health-agent --help`, `question --help`, and `telegram --help` were also invoked
locally to confirm the combined command tree. No branch was merged or pushed, and
no live credentials, external APIs, OAuth flows, or personal data were used.
