# Task 3 report — automatic Sunday training orientation

## Result

- Added profile-scoped `weekly-preferences` to dated weekly-plan prompts and persisted snapshots.
- Added stable ISO-week Sunday draft caching and a single combined reflection/draft notice.
- Kept proposal and acceptance separate; automatic and revised drafts never create or mutate an accepted plan.
- Added plain-text show/generate/revise routing while ordinary questions and availability constraints remain dialogue.
- Added conservative desired-slot fallback for provider failures without invented completion, intensity, pace, or weights.

## Verification

Strict RED-before-production-code was not performed; tests and implementation were
initially written in the same edit. The first focused run after that edit did fail and
provided an intermediate RED signal:

```text
$ .venv/bin/pytest -q tests/pilot/test_training.py
.F....FFF......                                                          [100%]
4 failed, 11 passed in 0.38s
```

Those failures identified compatibility gaps for accepted-plan Sunday reflection,
legacy revision prompt fields, and cached provider-failure reflection. After correcting
them, the final GREEN verification was:

```text
$ .venv/bin/pytest -q tests/pilot/test_training.py
................                                                         [100%]
16 passed in 0.40s

$ .venv/bin/ruff check src/health_agent/pilot/training.py tests/pilot/test_training.py
All checks passed!

$ .venv/bin/mypy src/health_agent/pilot/training.py tests/pilot/test_training.py
Success: no issues found in 2 source files

$ git diff --check
(no output)
```

## Self-review

- Scope is limited to `training.py`, `test_training.py`, and this required report.
- No owner-specific routine, goal, private activity read, live API call, or scheduling integration was added.
- Weekly notice delivery retains `training-weekly-{ISOyear}-W{week}` and therefore honors the existing delivery ledger.
- Draft generation is cached before delivery under `training-draft-{ISOyear}-W{week}` and reused after restart or provider outage.
- Notice length is capped at 1800 characters and preference-only weeks do not manufacture a retrospective.
