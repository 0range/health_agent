# Task 2 implementation report

## Summary

Implemented the framework-independent health-question application service and its
OpenAI Responses adapter. The application runs the local urgent-language guard before
either evidence retrieval or a remote call, builds profile-scoped context only through
the existing Task 1 boundary, and appends deterministic local sources and limitations.
Unavailable outcomes carry only closed safe error codes and generic user text.

The OpenAI adapter uses `responses.create` with `store=False`, a bounded output budget,
and a one-way SHA-256 profile safety identifier. It uses no conversations or response
chaining. It validates `status == "completed"` and non-empty `output_text`; provider
exceptions are suppressed with `from None` before crossing adapter boundaries.

`OPENAI_API_KEY` takes precedence over the default private key file
`.tokens/openai-api-key`. The fallback requires an exact `0600` regular file and opens it
with `O_NOFOLLOW`; neither secret values nor provider details reach public errors.
`gpt-5-mini` is the documented stable default and can be overridden with `OPENAI_MODEL`.

## Files

- `src/health_agent/questions/service.py`
- `src/health_agent/questions/openai.py`
- `src/health_agent/config.py`
- `pyproject.toml`
- `uv.lock`
- `tests/questions/test_service.py`
- `tests/questions/test_openai.py`
- `tests/questions/test_config.py`

## Verification

- `uv run pytest tests/questions/test_service.py tests/questions/test_openai.py tests/questions/test_config.py -q` — passed, 20 tests
- `uv run ruff check src/health_agent/questions src/health_agent/config.py tests/questions/test_service.py tests/questions/test_openai.py tests/questions/test_config.py` — passed
- `uv run mypy src/health_agent/questions src/health_agent/config.py` — passed
- `git diff --check` — passed

## Scope

No CLI, Telegram composition, live API call, OAuth action, token, personal record, or
network test was added. The next task owns transport composition.
