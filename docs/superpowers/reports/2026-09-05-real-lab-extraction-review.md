# Real laboratory extraction v0.1 — whole-branch independent review

Reviewed code range: `d52fb6f..20811a5`. Documentation was also checked through
`1e1b747`. Review date: September 5, 2026.

SPEC FAIL

QUALITY CHANGES

OVERALL NOT READY

The implementation has the intended conservative extraction and persistence
architecture, and the previously reported Task 1–3 defects are resolved. Two
medium-priority operator-path gaps still prevent an unattended installation from
completing the documented recovery and unmapped-analyte workflows without direct
database access or an optional Sheets installation. A lower-priority provenance
edge should also be corrected. No tests, application commands, live APIs,
credentials, health data, or implementation files were run or changed for this
review; only this report was authored.

## Findings

### R1 — MEDIUM: attention jobs cannot be identified or retried through the operator CLI

`lab-extract run` returns only aggregate counts, and `lab-extract status` exposes
configuration, aggregate state counts, and the daily budget
(`src/health_agent/lab_extraction/cli.py:61-80`; queue status is assembled at
`src/health_agent/lab_extraction/queue.py:129-152`). The retry command, however,
requires a document UUID (`cli.py:83-93`). Neither command exposes which document
and page needs attention or its fixed safe error code.

This breaks the production recovery loop for unattended imports. For example, a
scheduled Drive/Gmail page can reach `ocr_unavailable`, `cloud_outcome_unknown`,
`cloud_attempt_limit`, or `page_evidence_changed`; automation reports only the
profile job result and aggregate attention count. The operator cannot determine
which document to pass to `retry`, or whether `--acknowledge-unknown` is appropriate,
without querying `lab_extraction_jobs` directly or having retained undocumented
external bookkeeping. The runbook currently reinforces the count-only behavior
while presenting retry as the recovery route (`docs/lab-extraction.md:84-99`).

Add a bounded, content-free attention view, either as a status detail mode or a
separate command. It should emit only document UUID, page number, extractor version,
state, and a declared safe code; it must never print page text, evidence, filenames,
model output, or exception details. Add CLI and scheduled-import coverage proving an
operator can discover an unknown-outcome document, select the acknowledgment path,
and retry it while all lifetime/daily counters remain unchanged except for the new
reserved request.

### R2 — MEDIUM: unmapped analytes have no general CLI or Telegram mapping path

Extraction deliberately retains unknown analytes as `unmapped_...` and normalization
correctly refuses them until a human supplies a supported canonical analyte. The core
correction API supports that operation through its `canonical_name` argument
(`src/health_agent/importer.py:366-387`), and Google Sheets passes an edited canonical
name (`src/health_agent/google_sheets/decisions.py:203-212`). The local review CLI
accepts only value and unit (`src/health_agent/cli.py:414-428`), while Telegram's
`/correct` grammar and call also accept only value and unit
(`src/health_agent/telegram/review.py:26-28,40-45,160-166`).

Consequently, a profile using the required local CLI, or a Telegram-only profile,
can reject an unmapped result but cannot correct and verify it. It needs the optional
Sheets integration or direct Python/database intervention, although the extraction
guide says an explicit correction to a supported analyte/unit is required without
identifying a usable command (`docs/lab-extraction.md:72-75`). This is material
because retaining unknown analytes is a normal designed outcome, not corruption.

Expose canonical-name mapping through at least the local `review correct` command,
validate it by attempting only a declared analyte/unit normalization, and document
the exact workflow. If Telegram mapping is intentionally omitted, say so and direct
the user to the safe local command. Add an end-to-end regression from an unmapped
extracted candidate through canonical correction to a verified normalized row, plus
profile-isolation and unsupported-pair rejection cases.

### R3 — LOW: a recovered digital-text page can be labelled as OCR provenance

For an empty stored page, `read_page` first tries digital PDF text and returns it
without invoking Vision when available (`src/health_agent/lab_extraction/local.py:139-147`).
Publication nevertheless labels every newly filled empty `DocumentPage` as
`local_ocr` (`src/health_agent/lab_extraction/queue.py:317-320`). Current imports
normally leave empty pages only for scans, so this is primarily a historical/backfill
edge, but it can overstate OCR use when a newer local parser recovers digital text
from an atypical old blank page.

Use a truthful generic recovery method such as `local_text_or_ocr`, or return a typed
local method alongside the text. Add a digital-recovery regression. This does not
alter the candidate value/evidence and is not independently release-blocking.

## Spec compliance assessment

The main data path is otherwise coherent. Imported PDF/PNG/JPEG pages are discovered
from PostgreSQL after connector transactions commit. Existing page text is reused;
empty pages read only the profile-owned, content-addressed, hash-verified original.
Local rendering/OCR is byte-, pixel-, page-, text-, timeout-, path-, MIME-, and
temporary-permission-bounded. Originals are never sent to OpenAI.

Candidate validation preserves complete ordered source tokens, qualifiers, literal
references, printed flags, and contiguous excerpts. Numeric values are finite and
bounded; qualified values remain unparsed. Only declared analyte/unit pairs normalize.
Unambiguous printed ranges are retained in the source unit, and the updated docs
correctly warn that downstream must not compare those bounds directly with a converted
normalized value. Printed flags remain source evidence through correction rather than
being recomputed as diagnoses.

The Responses adapter is text-only and uses the official strict `text.format` JSON
Schema contract, `store=false`, configured model/reasoning/output cap, a 30-second
client timeout, zero SDK retries, no tools, and a hashed profile identifier. It requires
a completed, non-refusal message with real output-text content and revalidates exact
page evidence. Exceptions and malformed results become fixed content-free codes.
Cloud remains disabled unless explicitly enabled for the profile; construction and
status do not load a key.

The queue enforces composite profile/document/page ownership, current-version
processing, profile advisory serialization, short claim/reservation/publication
transactions, claim-token freshness, and atomic observation/review publication.
Cloud reservations consume the durable daily and page-lifetime budgets before the
request. Unknown outcomes require durable explicit acknowledgment, including across
extractor versions. The cumulative 40-candidate and three-cloud-request page caps also
survive version changes. Pending, verified, rejected, and superseded source rows all
participate in deduplication, so replay does not resurrect reviewed facts. Corrected
rows retain evidence and source flags; dates and conflicting-date attention remain
untouched.

Production automation discovers only enabled extraction profiles and orders all source
jobs before extraction and extraction before Sheets
(`src/health_agent/automation/runner.py:148-163`). Connector failures remain isolated,
and Telegram imports enter the same database for the next scheduled run. Extraction is
a separate subprocess job, not part of connector transactions. Its stdout/stderr are
reduced to job status by the existing runner; the extraction CLI itself emits UUIDs,
counts, booleans, and fixed codes rather than medical content.

Verified observations continue through the existing explicit approve/correct/reject
transitions and only normalized verified rows reach downstream history. The current
question/insight context does not consume the new range and flag fields; the design and
final documentation now state that this is future insight-layer work rather than an
implemented clinical interpretation.

## Quality and verification assessment

The implementation is split into narrow validation, local reader, Responses adapter,
durable queue, orchestration, CLI, and automation boundaries with injectable fakes.
Database sessions do not escape through ORM entities across external calls; claims use
plain snapshots. Result publication serializes on the same document ownership boundary
used by explicit review, and stale local/cloud claims are rejected. Migration `0008`
matches the models, adds composite ownership and state/counter constraints, and refuses
to downgrade over configuration, jobs, or retained source flags.

The author reports the final integrated gate at `20811a5`: 864 pytest tests passed with
five inherited PyMuPDF/SWIG deprecation warnings; Ruff, offline lock/diff checks, and
mypy over source plus new lab tests (109 files) are clean. Broad mypy still reports 13
inherited errors in four existing test files, accurately disclosed rather than called
clean. Those results were not independently rerun. Tests use synthetic fixtures, fake
OCR/OpenAI, and disposable PostgreSQL; “offline” means no live health/provider API use,
not that local database/container transport is absent. The composed automation test
uses a committed synthetic document rather than one single live-like connector-to-vault
fixture, but inspection finds compatible importer, page, vault, queue, review, and
projection boundaries.

## Live-only and operational concerns

- Native Apple Vision language/layout quality, valid-but-unusual JPEG/PDF behavior, and
  messy real laboratory report recall remain unmeasured. Use only non-sensitive owner-
  controlled acceptance fixtures first.
- Real model precision, refusal/incomplete frequency, latency, and billing are unknown.
  `store=false` is not a zero-retention guarantee; enabling cloud authorizes sending the
  bounded whole page text, which may contain personal data.
- The real macOS scheduled environment, `/usr/bin/swift`/Vision availability, OpenAI
  credential/model access, Drive/Gmail/Sheets accounts, and Telegram timing were not
  exercised by the synthetic gate.
- Abrupt process death can leave a private OCR temporary directory. Normal success and
  failure clean it up, but no TTL cleanup guarantee is implemented.
- The extracted source flag and source-unit reference bounds must not be treated as a
  fresh diagnosis or compared blindly with converted normalized values. No clinical
  interpretation or trend-significance validation is claimed.

Resolve R1 and R2 and add their focused offline regressions before another whole-branch
readiness review. R3 can be corrected in the same round without changing the extraction
safety model. Live acceptance remains a separate, explicitly authorized operator step.
