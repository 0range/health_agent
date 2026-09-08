## Task 2 report

Implemented the bounded numeric identity completion in the registry, normalization, importer approval coverage, and dashboard SQL. No source aliases, parser/PDF/service behavior, migrations, or live systems were changed.

### RED

Command:

```text
.venv/bin/pytest -q tests/lab_extraction/test_registry.py tests/test_labs.py tests/test_lab_dashboard.py -k 'completed_canonical or completed_source or dimensionless or missing_source_unit'
```

Result before implementation: `25 failed, 4 passed, 113 deselected`. The failures covered every new canonical identity, all three missing-unit dimensionless identities, and the dashboard join.

### GREEN

Command:

```text
.venv/bin/pytest -q tests/lab_extraction/test_registry.py tests/test_labs.py tests/test_importer.py tests/test_lab_dashboard.py
```

Result: `169 passed, 5 warnings` (existing PyMuPDF/Swig deprecation warnings).

Commands:

```text
.venv/bin/ruff check src/health_agent/lab_extraction/registry.py src/health_agent/labs.py src/health_agent/lab_dashboard.py tests/lab_extraction/test_registry.py tests/test_labs.py tests/test_importer.py tests/test_lab_dashboard.py
.venv/bin/mypy src/health_agent/lab_extraction/registry.py src/health_agent/labs.py src/health_agent/lab_dashboard.py
```

Results: Ruff `All checks passed!`; mypy `Success: no issues found in 3 source files`.

### Files

- `src/health_agent/lab_extraction/registry.py`: canonical-only identities, exact units, and the three-ID `None` guard.
- `src/health_agent/labs.py`: delegates missing-unit normalization to the narrow registry guard.
- `src/health_agent/lab_dashboard.py`: display labels, NULL-unit dimensionless join, unit-marker-free display, and finite pre-completion ownership SQL.
- Focused tests in `tests/lab_extraction/test_registry.py`, `tests/test_labs.py`, `tests/test_importer.py`, and `tests/test_lab_dashboard.py`.

### Self-review and concerns

The pre-completion detail SQL hash is pinned to the actual generator at `b2f11bb`: `07728b38ff401276ea4ef65480dcd2c8ca5398713b75d645ff3012bedb4e2583`. All earlier flags select the old join and omit Task 2 identities. Edited-query refusal remains covered by the existing ownership tests. Literal `source_unit=None` is preserved on approval while `normalized_unit='1'`; other missing units remain unsupported. No live database or Metabase calls were made; root retains the requested live SQL check and full-suite run.

### Round 1 review fix

Added the required `h.normalized_unit = '1'` predicate inside the new NULL-source-unit join branch. The SQL regression now includes a permitted `urine_ph` identity with an inconsistent `normalized_unit='mmol/L'` and proves it is excluded while the consistent row remains included.

RED command:

```text
.venv/bin/pytest -q tests/test_lab_dashboard.py -k dimensionless_chart_includes_only_permitted_null_units
```

Result before the fix: `1 failed, 38 deselected`; the inconsistent stored unit appeared in the chart.

GREEN/check command:

```text
.venv/bin/pytest -q tests/test_lab_dashboard.py
.venv/bin/ruff check src/health_agent/lab_dashboard.py tests/test_lab_dashboard.py
.venv/bin/mypy src/health_agent/lab_dashboard.py
```

Results: `39 passed, 5 warnings`; Ruff `All checks passed!`; mypy `Success: no issues found in 1 source file`.
