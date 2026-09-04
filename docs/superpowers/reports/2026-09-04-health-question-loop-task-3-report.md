# Health Question Loop — Task 3 Report

## Delivered

- Added `health-agent question ask --profile-id UUID QUESTION` and
  `health-agent question status --profile-id UUID`.
- Added `health-agent telegram run`, which reads a verified local bot
  credential and composes SQLite state, Bot API gateway, messenger, hardened
  update service, long poller, short-lived database context builder, and the
  OpenAI-backed question application service.
- The Telegram adapter passes only the profile ID established by the existing
  bound private identity route. It never derives an identity from message text.
- `/status` is a read-only count-only view. `/sync` performs no connector
  mutation and directs the user to the existing Gmail/WHOOP CLI commands.
- Runtime construction exposes injectable factories for every external
  boundary, including credential store, state, gateway, responder/application,
  messenger, update service, and poller.

## Attachment limitation

There is no existing Telegram-specific atomic medical inbox implementation in
this repository. The production default therefore consumes the already staged,
signature-validated stream and records `needs_attention`; its reply explicitly
states that the attachment was **not imported**. A deployment that supplies a
real `MedicalInbox` receives it unchanged through
`build_telegram_question_runtime(..., medical_inbox=...)`.

## Safety behavior

- CLI setup failures map to a stable unavailable response and nonzero exit.
- `telegram run` maps startup failures to `telegram_runtime_unavailable`, never
  prints its credential, and stops cleanly on `KeyboardInterrupt`.
- Status output contains only readiness, profile ID, source counts, and stable
  safe error codes—never tokens, questions, answers, or evidence values.

## Verification

Executed on 2026-09-04:

```text
uv run pytest tests/questions/test_composition.py tests/questions/test_cli.py \
  tests/questions/test_service.py tests/questions/test_openai.py \
  tests/questions/test_config.py tests/telegram/test_service.py -q
54 passed

uv run ruff check src/health_agent/questions/composition.py \
  src/health_agent/cli.py tests/questions/test_composition.py \
  tests/questions/test_cli.py
All checks passed!

uv run mypy src/health_agent/questions/composition.py src/health_agent/cli.py
Success: no issues found in 2 source files
```

`health-agent question --help` and `health-agent telegram --help` also expose
the intended commands without requiring credentials or network access.
