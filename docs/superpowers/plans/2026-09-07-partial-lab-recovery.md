# Partial Lab Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax. User approved execution and parallel independent work.

**Goal:** Retain strictly valid laboratory rows despite invalid neighboring rows and recover cached failed responses without new model calls.

**Architecture:** Add a bounded partial validation result alongside unchanged strict extraction APIs, integrate it with existing cloud service publication, and expose a guarded offline cached-response import. Root separately operates existing PDF geometry/date recovery after read-only probes; no new parser architecture.

**Tech Stack:** Python, SQLAlchemy/PostgreSQL, existing OpenAI/Yandex adapters, pytest.

## Global Constraints

- Every persisted candidate passes existing strict validation after existing source-name whitespace restoration. Never repair numeric tokens/units/references/excerpts or auto-verify.
- Preserve originals/page text/dates/review decisions, profile boundaries, profile lock and document-before-job lock order, lifetime 3 cloud attempts, 40 candidates per page across versions, daily default20 and current usage. No migration or queue identity bump.
- Partial pages remain needs_attention/cloud_partial_output; never silently complete a page from partial or cached salvage.
- No provider call, credential read, fake reservation or attempt/budget reset in cached import. Unknown outcomes require existing explicit acknowledgment; this workflow cannot bypass them.
- Existing strict extractor.extract() returns tuple and remains all-or-nothing; support legacy injected cloud fakes unchanged. Runtime can prefer extract_partial() when present.
- No medical data, captures or scratch in Git. Preserve unrelated docs/backlog.md, docs/research/, src/health_agent/panel/workflows.py.

## Task 1: End-to-end partial and cached extraction recovery

**Files:** modify src/health_agent/lab_extraction/{types.py,validation.py,openai.py,service.py,queue.py,cli.py}, src/health_agent/ai/yandex.py; create src/health_agent/lab_extraction/cached.py for bounded cached envelope parsing and operator orchestration if needed; tests/lab_extraction/test_partial_recovery.py, tests/ai/test_yandex.py, docs/lab-extraction.md. No registry/parser/panel changes.

**Interfaces:** introduce frozen `PartialExtraction(candidates: tuple[Candidate,...], rejected_count: int)` and shared `validate_partial_candidates(payload, text)`; outer payload exactly candidates:list length0..40, each individual item restored then existing validate_candidates({"candidates":[row]},text). Invalid items counted, never returned. Valid empty array is zero accepted/zero rejected; malformed outer payload raises. `extract_partial(profile_id,text)` on both providers uses one actual provider response and same envelope validity; preserve `.extract` strict API via shared bounded request/parse helpers, no duplicate provider requests.

Service prefers partial extraction when supported, otherwise wraps legacy tuple with rejected_count0. Queue.publish accepts `rejected_count=0`; cloud partial publish inserts accepted candidates atomically but finishes needs_attention with safe code cloud_partial_output if rejected_count>0, including zero accepted. Record explicit v3 method. Existing normal full/empty responses keep completed behavior. Existing RunReport counts remain accurate.

Cached command syntax: `lab-extract import-cached PROFILE_UUID DOCUMENT_UUID PAGE_NUMBER CAPTURE_PATH`. Capture JSON shape is existing root capture `{ "page_text": str, "response": { "choices": [{ "finish_reason":"stop", "message": {"role":"assistant", "content":JSON_STRING, ...}}], ...}}`. Reuse strict Yandex envelope checks; bound file to1MiB and output80k/text12k, reject symlinks/nonregular/unknown or truncated/refusal/toolcall envelopes; no token/settings provider consent needed for LOCAL parsing. Explicit owner/page must exist, text byte-for-byte identical to DocumentPage.extracted_text and job digest ifpresent. Only current needs_attention with cloud_invalid_output or cloud_partial_output; no automatic requeue. Require job has prior reserved cloudattempt and local_completed provenance. Profile lock + document/job locks; no mutation on invalidstate/text/envelope/candidate-limit. Cache-derived rows use existing review row structure with explicit lab_extraction_v3_cached reason and cached_structured_partial_v3 method; keep original model_name and spent counts, leave needs_attention/cloud_partial_output. Report inserted/accepted/rejected counts only. Repeating import is idempotent. Factor only shared insertion/limit/dedup logic needed to avoid copying queue.publish; do not manufacture a normal running claim or call reserve_cloud.

- [ ] RED focused tests with synthetic mixed payload: one valid Glucose row, one altered value; partial returns1/1, strict API stillraises; allinvalid0/1; validempty0/0; malformedouter/over40raises.

```python
good = {"source_name":"Glucose","source_value":"5.1","source_unit":"mmol/L","reference_text":"3.9 - 6.1","source_flag":None,"evidence_excerpt":"Glucose 5.1 mmol/L 3.9 - 6.1"}
bad = {**good, "source_value":"99"}
result = validate_partial_candidates({"candidates":[good,bad]}, good["evidence_excerpt"])
assert len(result.candidates) == 1 and result.rejected_count == 1
```

- [ ] Implement bounded per-row validation; share response requests and envelope parsing. Test Yandex/OpenAI each perform onecall, bad envelope neverpublishes, strict callers unchanged.
- [ ] RED disposablePG: one accepted cloudrow retained NEEDS_REVIEW and jobattention/codepartial, cloudattemptspentonce; local candidates counted correctly; ordinary accepted/emptycompleted remains. Implement publish integration with shared insertion helper and no broadrefactor.
- [ ] RED cached CLI/service: own matching failedpage producesreviewrow, keepsjobattention/attempts/budget; repeat inserts0; foreignprofile/page/text/digest/malformedenvelope/completed/unknownoutcome rejectwithoutmutation; missingpriorattempt reject; priorrejectedrow notresurrected;40limitrollsback. Implement and run those tests, including filebounds/symlink handling.
- [ ] GREEN `.venv/bin/pytest -q tests/lab_extraction tests/ai/test_yandex.py`; `.venv/bin/ruff check src/health_agent/lab_extraction src/health_agent/ai/yandex.py tests/lab_extraction/test_partial_recovery.py tests/ai/test_yandex.py`; `.venv/bin/mypy src/health_agent/lab_extraction src/health_agent/ai/yandex.py`. Report actual RED/GREEN; commit owned files only. No production writes/provider calls.

## Root operations and completion

- [ ] While worker implements, pure read-only geometry extraction on inventory pages; retain private manifest of concrete numeric candidates and missing date evidence. No duplicated audit already completed.
- [ ] Task review + final whole-change review, full pytest/Ruff/mypy. Then backup production and run new import-cached on exact-matching captures for likelylab pages; invalid/nonlab/unknown pages remain untouched. Store private per-page outcomes, no bulkclinicalverification.
- [ ] Use existing supported document-scoped PDF evidence persistence and conservative labelled date recovery only where read-only probe demonstrates valid geometry/date evidence; preserve immutable current source. Report supported rows/remaining failures rather than inventing dates or queuezeroing.
- [ ] Update short user-facing result with actual newcandidates vs verified counts and remaining gaps. Push verified commit to workingbranch and fast-forward main only if no unrelated remotechanges. User needs no action unless original genuinelyambiguous.

## Task 2: Proven gridded lab layout with a non-first header

**Files:** src/health_agent/pdf_lab_geometry.py, src/health_agent/lab_extraction/registry.py, tests/test_pdf_lab_geometry.py, tests/test_pdf_evidence.py, docs/pdf-lab-geometry.md. Disjoint from Task1 implementation; parallel allowed by user. No cloud/service/panel/schema changes.

**Context:** Read-only actual original shows a drawn five-column grid, but PyMuPDF combines the report heading and signature into the same table. The exact laboratory header is row2 rather than row0. Existing parser discards all rows. Pure geometry probe132pages found4supported and0newrows: repeating existingrepair is insufficient. No PHI in fixtures; generate syntheticPDF with Cyrillic-capable font, merged preamble and footer, exact headings only copied as format labels.

**Interfaces:** keep extract_lab_geometry/pdf_evidence APIs. Recognize complete header `("Параметр", "Значение", "Ед. измер.", "Реф.значение", "Представление")` mapping `(name,result,unit,reference,comment)`. `_grid_rows` can locate one exact complete supported header after merged preamble rows; require unambiguous header and aligned ordered physical cell columns, reject incompatible/multiple mappings; do not interpret merged preamble/footer/narrative as rows. Preserve old supported formats unchanged. Source representation column stored as comment only: no inference of H/L/* from `[-*-]` or `[---]*`. For newly supported layout use explicit geometry method `pdf_table_v2` so immutable stored v1 evidence is never overwritten; pages using only original supportedlayouts retain v1 method. Required mapped cells and numeric/unit validation remain strict.

Registry exactaliases only: total `prolactin` additionally `Пролактин / Prolactin`; distinct new `monomeric_prolactin` aliases `Пролактин мономерный (пост ПЭГ)|Пролактин мономерный (пост-ПЭГ)|Monomeric prolactin`, never collapse total/monomeric/macro. For both add separate literal unit family `mU/L` with alias `мЕд/л`; do NOT silently merge this new family with mIU/L/uIU/mL or mass units. Monomeric also accepts existing explicit mIU/L/uIU/mL/ng/mL unitfamilies without conversion. Preserve source names/units. No inferdates from signature or blankcollectionlabel; new rows NEEDS_REVIEW.

- [ ] RED synthetic grid with2mergedpreamble rows, supportedRussianheader, two arbitrary plausible syntheticnumericrows and mergedfooter; assert2source-proven rows, correctfieldmapping,commentnotflag, exactsourcehash, v2method. Existing firstheader fixtures stillv1.
```python
assert canonical_name("Пролактин / Prolactin") == "prolactin"
assert canonical_name("Пролактин мономерный (пост ПЭГ)") == "monomeric_prolactin"
assert normalize_registered("prolactin", "123", "мЕд/л")[1] == "mU/L"
```
- [ ] Add negative synthetic missing/duplicate/misorderedheaders, mergedrequiredcells, competingheaders and footer numerictext; no guessedrowownership. Keep unknown/incompatibleunit rows rejected. Implement only exactlayout/aliases, no broadOCR/parser redesign.
- [ ] DisposablePG persistence via existingpersist_pdf_evidence: twoNEEDS_REVIEW rows attachedv2PageEvidence; repeatedruninserts0, oldpage_textanddatesunchanged, noverification; preserveexistingv1evidence. Syntheticonly.
- [ ] GREEN `.venv/bin/pytest -q tests/test_pdf_lab_geometry.py tests/test_pdf_evidence.py tests/lab_extraction/test_local.py` (locate actual registry testfile ifnamedotherwise), Ruffchangedfiles,mypy sourcepdfgeometry/registry. Commitownedonly; report RED/GREEN and concerns to task-2-report.md; no liveops.
