# Task 1 report: standalone sleep diary capture

## Scope

Changed only `src/health_agent/pilot/sleep.py` and `tests/pilot/test_sleep.py` for implementation and tests. No private diary text, live provider call, Telegram call, store mutation, registry/dashboard, or workflow change was made.

## RED

Initial environment check:

```text
$ pytest -q tests/pilot/test_sleep.py
zsh:1: command not found: pytest
```

The project virtual environment was then used:

```text
$ .venv/bin/pytest -q tests/pilot/test_sleep.py
7 failed, 21 passed in 0.12s
```

The seven failures were the synthetic standalone report, five supported personal sleep forms, and provider-failure confirmation. The negative cases passed while still exercising questions with and without `?`, a general sleep claim, a third-person report, unrelated health text, an unknown slash command, and a command.

## GREEN

```text
$ .venv/bin/pytest -q tests/pilot/test_sleep.py tests/pilot/test_sleep_grounding.py tests/pilot/test_runtime.py
70 passed, 5 warnings in 2.12s
```

The warnings are pre-existing SWIG deprecation warnings emitted during imports.

```text
$ .venv/bin/ruff check src/health_agent/pilot/sleep.py tests/pilot/test_sleep.py
All checks passed!

$ .venv/bin/mypy src/health_agent/pilot/sleep.py tests/pilot/test_sleep.py
Success: no issues found in 2 source files
```

## Behavior and self-review

- Conservative standalone recognition covers personal/implicit-person sleep, waking, nighttime rising, restedness, and dream reports.
- Questions, generic claims, third-person reports, commands, unknown slash commands, and unrelated symptoms remain non-diary.
- Existing morning answers retain their delivered `morning_prompt_key`; standalone entries do not invent one.
- Existing durable user text remains authoritative on retry, and diary persistence still happens before the provider call.
- The deterministic `Запись сна сохранена.` prefix is based on finding the durable diary row, is added once, and the combined reply remains within the existing 1200-character limit.
- Active causal sleep follow-ups remain on the reviewed grounded causal path instead of being reclassified as standalone diary entries.
- Empty voice transcription remains unsaved.

No PHI was added to tests or tracked files.
