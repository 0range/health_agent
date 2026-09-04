# Management panel final-fix report

## Status

Complete. The final review findings I-1, I-2, M-1, and M-2 are fixed and
covered by tests. No live credential, user data, OAuth flow, synchronization,
connector network operation, or external network resource was accessed.

## Changes

- The pure panel application rejects requests unless `Host` exactly matches the
  canonical IPv4 loopback authority for its configured port. The live adapter
  passes the actual ephemeral bound port to the application and fails closed on
  missing or duplicate Host headers. Rejections use a fixed Russian message and
  never echo hostile input.
- Canonical HTTP port 80 uses `127.0.0.1` and `http://127.0.0.1`, matching normal
  browser Host and Origin serialization without accepting alternate spellings.
- Connector cards now carry only local, profile-scoped account identifiers.
  Guidance uses the card status, safe error code, and account cardinality:
  healthy cards say no action is required; single-account WHOOP/Gmail commands
  identify that account; multi-account WHOOP directs the user to per-account
  local status with an explicit placeholder rather than silently targeting
  `main`; multi-account Gmail uses its safe all-account status command.
- All closed connector detail strings rendered by production panel adapters are
  Russian. Machine status and error codes remain unchanged.
- Added pure and real-adapter hostile-Host GET/POST tests, a successful
  ephemeral-port GET/POST test, port-80 Host/Origin tests, a connector
  state/action matrix, account-context coverage, and Russian-copy coverage.

## RED evidence

Before implementation, the new tests could not collect because the card model
did not expose account context:

```text
uv run pytest -q tests/panel/test_http.py tests/panel/test_service.py

==================================== ERRORS ====================================
__________________ ERROR collecting tests/panel/test_http.py ___________________
tests/panel/test_http.py:264: in <module>
    ConnectorCard("whoop", "not_connected", "", account_ids=()),
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   TypeError: ConnectorCard.__init__() got an unexpected keyword argument 'account_ids'
=========================== short test summary info ============================
ERROR tests/panel/test_http.py - TypeError: ConnectorCard.__init__() got an u...
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.21s
```

## Focused panel gates

```text
uv run pytest -q tests/panel
............................................                             [100%]
=============================== warnings summary ===============================
<frozen importlib._bootstrap>:488
<frozen importlib._bootstrap>:488
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute

<frozen importlib._bootstrap>:488
<frozen importlib._bootstrap>:488
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type SwigPyObject has no __module__ attribute

<frozen importlib._bootstrap>:488
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type swigvarlink has no __module__ attribute

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
44 passed, 5 warnings in 0.31s
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute

uv run ruff check src/health_agent/panel tests/panel
All checks passed!

uv run mypy src/health_agent/panel tests/panel
Success: no issues found in 8 source files
```

## Repository gates

```text
uv run pytest -q
........................................................................ [ 22%]
........................................................................ [ 44%]
........................................................................ [ 66%]
........................................................................ [ 88%]
....................................                                     [100%]
=============================== warnings summary ===============================
<frozen importlib._bootstrap>:488
<frozen importlib._bootstrap>:488
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute

<frozen importlib._bootstrap>:488
<frozen importlib._bootstrap>:488
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type SwigPyObject has no __module__ attribute

<frozen importlib._bootstrap>:488
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type swigvarlink has no __module__ attribute

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
324 passed, 5 warnings in 4.39s
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute

uv run ruff check .
All checks passed!

uv run mypy src/health_agent/panel tests/panel
Success: no issues found in 8 source files

uv run alembic heads
0005_whoop (head)

git diff --check
(exit 0, no output)
```

Full-repository mypy remains at the documented, unchanged Gmail baseline:

```text
uv run mypy src tests
tests/gmail/test_vault_importer.py:48: error: Argument 1 to "Path" has incompatible type "str | None"; expected "str | PathLike[str]"  [arg-type]
tests/gmail/test_stores.py:101: error: Item "None" of "SeenAttachment | None" has no attribute "status"  [union-attr]
tests/gmail/test_service.py:308: error: Item "None" of "SeenAttachment | None" has no attribute "status"  [union-attr]
tests/gmail/test_service.py:318: error: Item "None" of "SeenAttachment | None" has no attribute "status"  [union-attr]
tests/gmail/test_service.py:356: error: Item "None" of "SeenAttachment | None" has no attribute "status"  [union-attr]
tests/gmail/test_service.py:498: error: Item "None" of "SeenAttachment | None" has no attribute "filename"  [union-attr]
tests/gmail/test_api.py:6: error: Library stubs not installed for "httplib2"  [import-untyped]
tests/gmail/test_api.py:6: note: Hint: "python3 -m pip install types-httplib2"
tests/gmail/test_api.py:6: note: (or run "mypy --install-types" to install all missing stub packages)
tests/gmail/test_api.py:9: error: Skipping analyzing "googleapiclient.errors": module is installed, but missing library stubs or py.typed marker  [import-untyped]
tests/gmail/test_api.py:9: note: See https://mypy.readthedocs.io/en/stable/running_mypy.html#missing-imports
Found 8 errors in 4 files (checked 86 source files)
```

## Self-review and concerns

Host validation precedes target parsing and all route dispatch in the pure
application. The live handler preserves that check for normal, oversized, and
unsupported-method requests and rejects ambiguous duplicate Host values. Port
80 tests independently prove canonical Host acceptance and non-canonical Origin
rejection. Account identifiers originate from already profile-scoped WHOOP and
Gmail configuration reads, and all rendered values remain HTML-escaped.

The only concern is the unchanged full-repository mypy baseline above. It is
limited to pre-existing Gmail tests and missing third-party stubs; no Gmail code,
tests, or dependencies were changed.
