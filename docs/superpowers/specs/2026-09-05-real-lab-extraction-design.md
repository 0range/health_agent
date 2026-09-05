# Real laboratory extraction v0.1

Date: 5 September 2026. Base: `codex/v1-slice-1@18d97a3`.

## Outcome and chosen tradeoff

Turn already imported PDF/image pages into evidence-backed laboratory candidates
that the existing Telegram/CLI/Sheets review can resolve. PostgreSQL and the
immutable original vault stay authoritative. Extraction never verifies a fact,
changes an existing observation, invents medical dates, or sends an original file
to a cloud service. WHOOP remains the v0.1 weight source.

Choose local digital text / on-device OCR first, then an explicitly enabled,
profile-scoped OpenAI **text-only** structured fallback for unresolved layouts.
Always-cloud processing would unnecessarily export health text and spend money;
only expanding the old aliases would still miss real multiline Russian/English
reports. The chosen approach supports useful deterministic extraction without
credentials, with a conservative cloud path for messy documents.

## Boundaries and evidence

Reuse Document, DocumentPage, LabObservation, ReviewItem and FileVault. Nonempty
stored page text is reused unchanged. Empty pages can be read/rendered from the
profile-owned original and OCRed locally; no evidence text already backing an
observation is overwritten. Vault input must match the document hash, canonical
content-addressed path and supported PDF/PNG/JPEG MIME; reject symlinks and files
over 50 MiB. Rasterized images are bounded to 25 million pixels. Local OCR uses a
fixed Apple Vision subprocess with a 30-second timeout and private temporary
files removed on normal success/failure. Missing OCR is `ocr_unavailable`, never
an empty successful extraction. Abrupt process death can leave private temporary
files; this is documented and not represented as a TTL guarantee.

Each candidate retains exact source name/value/unit, page number, a contiguous
source excerpt (at most 500 characters), optional literal reference text, local
or cloud method, and extraction version. Every named field must be present in
that page's excerpt. Values are bounded finite decimals; inequality-qualified
values remain unparsed and cannot be approved as exact numbers. Unknown analytes
are retained with an explicit unmapped canonical key and require mapping through
the existing explicit correction path before normalization. Unknown units are
retained verbatim but cannot be silently converted. An extensible registry covers
CBC, liver/kidney/chemistry, electrolytes, inflammation, thyroid/hormones and
vitamins beyond the existing aliases. Only declared analyte/unit pairs normalize.
No physiological reference range or sex/age-specific interpretation is invented.
Unambiguous printed low-high ranges populate the existing numeric reference
fields; ambiguous ranges stay text-only. A nullable `source_flag` on the source
observation preserves a printed H/L/arrow/star, never a newly inferred diagnosis.
Existing explicit review produces normalized values through the registry.
The downstream insight layer, not extraction, decides how to describe confirmed
reference comparisons, overall picture, trend significance and safe next steps.

## Queue, restart and cost

Two small tables: profile configuration/budget and per-document-page extraction
jobs. Jobs have composite profile/document/page foreign keys and a unique
document/page/extractor-version identity. A separate worker discovers imported
pages; connector transactions never call OpenAI. Profile-scoped PostgreSQL
advisory locks serialize workers, while short transactions publish claim tokens,
request reservations and results. Result publication verifies the current claim.

Local interrupted work can restart. A durable `cloud_in_flight` reservation is
written **before** sending a request. Crash/timeout outcomes become
`cloud_outcome_unknown` and are never automatically retried. Explicit per-document
retry is required, with a separate acknowledgment flag for unknown cloud outcomes.
At most three cloud requests per page lifetime are allowed. Profile daily cloud
budgets survive restart; default 20 requests/day, hard maximum 100. Default worker
batch is four pages (hard maximum 20) and at most two cloud calls (hard maximum
10). Queued cloud work waits when a budget is exhausted. Candidate deduplication
under the document lock considers existing pending, verified, rejected and
superseded source rows, so backfills cannot resurrect or duplicate reviewed facts.

Local page text is bounded to 60000 characters, cloud input to 12000, and output
to 40 candidate rows. Over-limit pages stay explicitly unresolved; no silent
truncation is called complete. Cloud fallback reuses configured OpenAI key/model,
reasoning effort and output-token limit; official Responses uses strict
`text.format` JSON Schema, `store=false`, 30-second timeout, no SDK retries, no
tools, and a hashed profile safety identifier. Only completed, non-refusal output
passing schema and exact-evidence validation can produce candidates. Refusal,
incomplete output and malformed results have content-free safe error codes.

The document text is untrusted data, not instructions. Prompt injection cannot
select another profile, access tools, alter review status or forge evidence not
present in the submitted page. Health text is sent only after explicit profile
cloud enablement. `store=false` is not a claim of zero provider retention.
API contract reference: [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

## CLI and scheduled production composition

`lab-extract configure PROFILE [--openai/--no-openai]` enables the profile worker;
cloud defaults off. `lab-extract run PROFILE [--limit N]` discovers and processes
bounded new/backfill pages. `lab-extract status PROFILE` prints queue counts,
safe states and request budget only. `lab-extract retry PROFILE DOCUMENT
[--acknowledge-unknown]` requeues eligible unresolved pages without resetting cost
counters or rewriting facts. Configuration permits disabling the worker.

Configured profiles are discovered by the existing automation registry. The
extraction job runs after source syncs (Drive/Gmail/WHOOP), before downstream
projections when combined with Sheets, and also catches newly committed Telegram
documents on the next schedule. It does not couple Telegram latency to cloud
extraction. No live configuration is changed during implementation.

## Verification and exclusions

TDD uses synthetic messy Russian/English text/raster/PDF fixtures, fake OCR and
Responses, and disposable PostgreSQL. Cover exact evidence, broad aliases,
unknown units/qualifiers, profile isolation, dedupe after human review, restart,
stale claims, unknown external outcome, budgets, no-network automation discovery,
CLI redaction and migration integrity. Full offline pytest/Ruff/mypy/lock/diff
gates and independent review are required before integration.

No diagnoses, automated medical interpretation, automatic fact publication,
cloud image upload, historical observation rewrites, live API calls or personal
data are in scope. Real OCR/model extraction quality still requires a later
owner-controlled non-sensitive acceptance exercise.
