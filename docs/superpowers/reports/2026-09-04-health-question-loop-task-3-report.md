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

## Review corrections

- `telegram run` now idempotently registers the locally verified bot namespace
  before constructing the poller and validates bot identity/webhook state before
  it prints `status=running`.
- The default Telegram inbox now writes only a private transient copy of the
  fully staged attachment, rejects symlinked temporary roots, and imports
  validated PDFs through `FileVault` and `import_document` with the bound
  `profile_id` and Telegram `source_external_id`. It removes transient bytes on
  every success or failure path. Replays retain importer provenance and return a
  truthful duplicate receipt; validated non-PDFs fully consume/hash then return
  `needs_attention` without claiming an import.
- `/sync` now names the bound profile and uses the actual commands:
  `health-agent gmail sync PROFILE_UUID` and
  `health-agent whoop sync --profile-id PROFILE_UUID`.
- Database-backed question context rejects unknown profiles. Status checks the
  same local responder construction used by `ask`, and both status setup and
  post-start poller failures emit stable, secret-free CLI codes. Its
  `readiness=local` field deliberately does not claim a remote OpenAI probe.

## Safety behavior

- CLI setup failures map to a stable unavailable response and nonzero exit.
- `telegram run` maps startup failures to `telegram_runtime_unavailable`, never
  prints its credential, and stops cleanly on `KeyboardInterrupt`.
- Status output contains only readiness, profile ID, source counts, and stable
  safe error codes—never tokens, questions, answers, or evidence values.

## Verification

Executed on 2026-09-04:

```text
uv run pytest tests/questions/test_composition.py tests/questions/test_cli.py -q
15 passed

uv run ruff check src/health_agent/questions/composition.py \
  src/health_agent/cli.py src/health_agent/telegram/service.py \
  tests/questions/test_composition.py tests/questions/test_cli.py
All checks passed!

uv run mypy src/health_agent/questions/composition.py src/health_agent/cli.py \
  src/health_agent/telegram/service.py
Success: no issues found in 3 source files
```

`health-agent question --help` and `health-agent telegram --help` also expose
the intended commands without requiring credentials or network access.
