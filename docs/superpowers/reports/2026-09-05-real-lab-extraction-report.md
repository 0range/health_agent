# Real laboratory extraction v0.1 — implementation report

Branch: `codex/v1-real-lab-extraction`; isolated worktree `health-agent-lab-extraction`.
No main merge/push, live data/credentials/API call or live configuration change.

## Delivered

- Local PDF text/Apple Vision OCR route plus optional explicit per-profile official
  Responses text-only structured fallback. Extensible Russian/English analyte/unit
  registry, source-token/evidence validation and conservative numeric handling.
- Literal references, numeric unambiguous range bounds, printed flags, heuristic
  confidence, immutable page/document/source provenance. No inferred dates/ranges,
  diagnosis or automated verification: every new observation requires review.
- Migration0008 after Sheets0007, profile configuration/budgets and versioned page
  queue, advisory worker serialization, claim-token guarded short transactions,
  durable request reservations/unknown acknowledgment and page-lifetime caps.
- Idempotent backfill/restart; existing verified/rejected/corrected source rows
  never mutated or resurrected. Atomic cumulative40candidate cap and3cloud-request
  cap survive extractor-version changes; no silent truncation.
- `lab-extract configure/run/status/retry` CLI and existing scheduled automation
  integration: connectors → extraction → Sheets; newly committed Telegram imports
  enter the next bounded run. Existing question/PDF/capture paths preserved.
- Operator documentation: [lab-extraction.md](../../lab-extraction.md).

## Verification and review status

Task1/2 independently SPEC PASS / QUALITY APPROVED after evidence association,
malformed Responses and OCR limit fixes. Task3 independently SPEC PASS / QUALITY
APPROVED after cumulative row/page-lifetime cost fixes. Task4 and whole-branch
review are pending at this report's initial creation; final gate/verdict appended
below when actually obtained.

Full offline gate at integrated main4d5eca9 plus implementation/fixes:
861pytest passed,5inherited PyMuPDF/SWIG deprecation warnings;2additional focused
corrected-source/stale-cloud tests passed. Ruff clean; mypy src plus new tests109
files clean; offline lock and diff clean. Broad mypy src tests still reports13
inherited errors in4existing test files (automation/test_runner.py,
google_drive/test_api.py,google_drive/test_oauth.py,test_staging.py).

## Intentional boundaries / later acceptance

Native Apple Vision OCR language/layout quality, real model extraction precision,
latency and billing are not claimed from mocks. Live acceptance is separate and
requires owner authorization. Cloud defaults off and transmits the limited whole
page text when enabled; store=false is not zero provider retention. Abrupt death
can leave private OCR temporary files; no TTL cleanup guarantee.

WHOOP remains the weight source. Extraction supplies data to the insight layer;
overall medical interpretation, clinical significance of trends and safe next
steps are not implemented here. Missing date/ambiguous range/unknown unit remain
explicitly unresolved, never fabricated to improve coverage.

Numeric reference bounds remain in source_unit; downstream must not compare them
blindly with a converted normalized_value. Printed source_flag is source evidence,
including in correction lineage, not a recomputed abnormality flag. Current
question context consumes verified normalized values but does not yet expose
these newly available range/flag fields; extending that is insight-layer work.

## Final integrated implementation gate —20811a5

Latest requested main `d52fb6f` merged cleanly as `20811a5`, preserving Russian
Telegram UX and the lab integration. Full offline pytest: **864passed**,5inherited
PyMuPDF/SWIG deprecation warnings. Ruff clean; mypy src/new lab tests109files clean;
offline lock and diff clean. Broad mypy's13inherited errors remain as listed above.
The full migration roundtrip, metadata comparison, CLI and post-sync integration
tests are included in this run. Independent whole-branch review requested against
`.superpowers/sdd/2026-09-05-real-lab-extraction/review-d52fb6f..20811a5.diff`;
no readiness verdict is claimed until that review returns.

## Whole-review fix round

Whole review b737088 requested two medium fixes (operator attention diagnostics,
local unknown-analyte mapping), plus one low provenance-label fix. All three
were reproduced as RED tests and implemented:

- `lab-extract status PROFILE --details [--limit 1..100] [--offset N]` emits bounded,
  profile-scoped current-version unfinished document/page IDs, state and declared
  safe codes. Unknown stored codes are redacted. Scheduled-import→unknown-outcome
  discovery→explicit acknowledged retry is covered without resetting counters.
- `review correct OBS --profile-id PROFILE --value VALUE --unit UNIT
  --canonical-name CANONICAL` exposes the existing explicitly verified correction
  path, retaining source lineage and declared normalization/profile guards.
  Unsupported mapping and foreign profile fail without changes. Telegram mapping
  remains intentionally absent; docs direct those users to local CLI or Sheets.
- Newly recovered page text uses truthful generic `local_text_or_ocr` provenance.

Post-fix full offline gates: **868pytest passed**,5inheritedSWIGwarnings; Ruff clean;
mypy src/new lab tests109files clean; lock/diff clean. Focused lab suite82passed.
No live data/config/API/credentials were used. Bounded final re-review is pending.

## Final independent verdict —95d6e21

Final reviewer verified6f86c4e and committed the report as95d6e21:
**SPEC PASS · QUALITY APPROVED · OVERALL READY · Task4 APPROVED.**
All whole-review R1–R3 findings resolved; no new high/medium findings.
See [independent review](2026-09-05-real-lab-extraction-review.md).

Delivered implementation remains6f86c4e with the868-pass gate above; subsequent
commits are reports/docs only. Migration0008 follows0007_google_sheets. Branch is
ready for owner-controlled integration, not merged/pushed here. Native OCR/model
quality, live credentials/privacy/billing and the downstream clinical insight
layer remain the explicitly documented acceptance/scope boundaries.
