# Bootstrap unit-alias normalization report

## Change

- Added only exact printed spelling aliases for the existing `10^9/L` and `U/L` unit families.
- Confirmed the pre-existing TSH alias normalizes `мкМЕ/мл` to `uIU/mL`.
- Did not add analytes, canonical-name aliases, conversions, parser behavior, schema changes, or data writes.
- Source value and unit strings remain unchanged at the normalization call boundary.

## RED / GREEN

- RED (initial command without worktree `PYTHONPATH`): 5 new alias cases failed because the editable environment resolved the main checkout; this was an environment-path issue, not a code result.
- GREEN: `PYTHONPATH=src .../pytest tests/test_labs.py -q` — 34 passed.
- GREEN: `.../ruff check src/health_agent/lab_extraction/registry.py tests/test_labs.py` — all checks passed.
- GREEN: `MYPYPATH=src .../mypy src/health_agent/lab_extraction/registry.py` — success, no issues.

## Boundary coverage

- Accepted exact aliases: `тыс/мкл`, `*10^9/л`, `10*9/литр`, `10^9/литр`, `Ед./л`, plus already-supported `мкМЕ/мл` and `Ед/л`.
- Rejected similar wrong units for blood-cell counts and enzymes.
