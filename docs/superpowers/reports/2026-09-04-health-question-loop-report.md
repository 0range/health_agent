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
