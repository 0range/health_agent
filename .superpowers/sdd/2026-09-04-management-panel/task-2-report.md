# Task 2 report: loopback HTTP page and safe local actions

## Status

Completed. The panel now has a deterministic `PanelApplication.handle()` route
boundary and a stdlib `ThreadingHTTPServer` factory restricted to `127.0.0.1`.

## RED / GREEN evidence

RED:

```text
uv run pytest -q tests/panel/test_http.py
ModuleNotFoundError: No module named 'health_agent.panel.http'
```

GREEN:

```text
uv run pytest -q tests/panel/test_http.py tests/panel/test_service.py
18 passed in 0.17s

uv run ruff check src/health_agent/panel tests/panel
All checks passed!

uv run mypy src/health_agent/panel tests/panel
Success: no issues found in 6 source files

git diff --check
exit 0
```

## Files

- `src/health_agent/panel/http.py`
- `tests/panel/test_http.py`
- `.superpowers/sdd/2026-09-04-management-panel/task-2-report.md`

## Implementation and self-review

- `GET /`, canonical `GET /profiles/<uuid>`, and `POST /profiles` are handled
  in-process with no socket, OAuth, token, or sync dependency.
- HTML is Russian-language, escaped, responsive, accessible, self-contained,
  and carries no external assets. Connector cards have safe display fields and
  static CLI guidance; connector operations have no clickable network action.
- POST parsing is limited to 4096 bytes, accepts only the expected two form
  fields, uses a per-application CSRF token, rejects cross-origin origins, and
  redirects after successful creation.
- Every response has no-store and browser hardening headers. Routing rejects
  query/fragment variants and noncanonical UUID paths. The server factory
  refuses every host except exact `127.0.0.1`.

## Concerns

No open concerns. The HTTP page relies on Task 1's closed, secret-free panel
view models; it deliberately does not invoke OAuth, sync, connector APIs, or
read credentials.

## Fix round 1: exact mutation origin and adapter method parity

- `PanelApplication` now derives its sole permitted mutation origin from its
  loopback port. A POST requires an `Origin` header byte-for-byte equal to
  `http://127.0.0.1:<actual-bound-port>` as well as its CSRF token. Missing
  origins and another loopback port are rejected.
- `serve_panel()` creates the application after binding so an ephemeral port
  also receives its actual origin. Its request handler dispatches every
  otherwise unsupported verb through `PanelApplication`, preserving the
  deterministic `405` and `Allow` response instead of stdlib `501`.
- Added focused regressions for a valid CSRF token with no origin, a different
  loopback port, and a real adapter-level `PUT /` request.

Fix-round verification:

```text
uv run pytest -q tests/panel/test_http.py tests/panel/test_service.py
19 passed in 0.19s

uv run ruff check src/health_agent/panel tests/panel
All checks passed!

uv run mypy src/health_agent/panel tests/panel
Success: no issues found in 6 source files

git diff --check
exit 0
```

Self-review: the expected origin is constructed only from the fixed loopback
host and validated port, including a kernel-assigned port. The adapter test
confirms the live handler exposes the same 405/Allow behavior as the pure
dispatcher. No open concerns.
