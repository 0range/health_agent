# Task 1 implementation report

## Summary

Implemented the read-only question-evidence boundary and local urgent-language guard.
Every SQLAlchemy retrieval statement explicitly predicates on `profile_id`; lab evidence
requires a verified review status, and WHOOP evidence comes only from normalized tables.
Evidence items contain only citation, source, time, metric, value, and unit—never raw
payloads, external identifiers, account fields, filenames, or document text.

## Files

- `src/health_agent/questions/__init__.py`
- `src/health_agent/questions/models.py`
- `src/health_agent/questions/context.py`
- `src/health_agent/questions/safety.py`
- `tests/questions/__init__.py`
- `tests/questions/test_context.py`
- `tests/questions/test_safety.py`

## Verification

- `uv run mypy src/health_agent/questions` — passed
- `uv run ruff check src/health_agent/questions tests/questions` — passed
- `uv run pytest tests/questions -q` — passed, 10 tests
- `git diff --check` — passed before commit

## Commit

`3727cdc feat: add profile-scoped question evidence`

## Concerns

The Task 1 boundary is intentionally framework-independent. The next task must invoke
`urgent_response()` before making a responder call and must append citations itself rather
than trusting generated output. No network, credentials, raw health records, or logs were
introduced.

## Fix round — independent-review remediation

### Verdict

**FIXED — focused evidence, safety, lint, and type gates pass.**

### Changes and review evidence

- Recovery evidence now joins normalized sleep, then normalized cycle, on matching
  `profile_id` and `connection_id`; it windows, orders, and cites the associated
  physiological start time. The joins use linkage fields internally only and never expose
  `sleep_id`, `cycle_id`, or another external identifier. No migration was needed.
- `WhoopBodyCurrent` is rendered as a current `sync_as_of` snapshot, not an observation.
  `weight_trend` includes the stable limitation code
  `weight_trend_insufficient_history` and user-safe wording that fewer than two dated
  measurements are available, so change cannot be established.
- Laboratory evidence now prefers `collected_date` over `issued_date`.
- The local bilingual urgent guard covers direct high-confidence chest-pressure/tightness,
  breathing, self-harm, and Russian inflection forms while declining generic informational
  prompts such as “What causes chest pain?”.
- Regression tests cover old/newly-updated and recent/oldly-updated recoveries, cycle
  fallback, profile/connection join isolation, stale body snapshots, dual lab dates, and
  bilingual positive/negative urgent language.

### Verification

- `uv run pytest tests/questions tests/whoop/test_normalize.py -q` — passed, 35 tests
- `uv run ruff check src/health_agent/questions src/health_agent/whoop/models.py tests/questions` — passed
- `uv run mypy src/health_agent/questions src/health_agent/whoop/models.py` — passed
- `git diff --check` — passed

### Implementation commit

`25d401a fix: correct question evidence provenance`
