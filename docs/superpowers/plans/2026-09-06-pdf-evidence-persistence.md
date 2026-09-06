# PDF evidence persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Put source-proven PDF table rows into existing import/review/Sheets/dashboard workflows without corrupting original page text.
**Architecture:** Immutable alternate page evidence table plus nullable observation evidence FK; one shared bounded persistence operation for new imports and explicit archive repair.
**Tech Stack:** Existing Python/PostgreSQL/SQLAlchemy/PyMuPDF; no new dependencies.

## Global Constraints

- Preserve DocumentPage.extracted_text, original PDF bytes, existing observations and review decisions.
- New geometry observations are NEEDS_REVIEW, never implicitly VERIFIED. No automatic rejection of legacy rows in this task.
- Exact same-profile document/page/hash linkage, local vault root and regular-file checks; no arbitrary stored path reads or PHI logging.
- No cloud calls, OAuth, budget/version resets or production mutations during task.
- Own migration `0013_pdf_table_evidence` down_revision `0011_medical_workflows`; parallel Calendar owns0012, root merges schema heads after review.
- User approved autonomous parallel implementation; no new approval questions or user tests.

### Task 1: Persist immutable evidence in import and bounded repair

**Files:** new `src/health_agent/pdf_evidence.py`, migration `alembic/versions/0013_pdf_table_evidence.py`, covering `tests/test_pdf_evidence.py`; modify models.py, importer.py narrowly, schema metadata/tests and docs/pdf-lab-geometry.md. Avoid cli.py/config.py/panel while Calendar task owns them; export repair API for root CLI wiring.

**Interfaces:** consume GeometryPage/Row/Cell from pdf_lab_geometry, parse_page_candidates and `_observation_from_candidate` equivalent shared construction (avoid import cycle). Produce `persist_pdf_evidence(session, document_id, *, profile_id, pdf_bytes)` and `repair_pdf_evidence(session, vault, *, profile_id, limit=150, apply=False)` with immutable aggregate reports (scanned/supported_pages/inserted/duplicates/blocked), no clinical values. Persist API called after real DocumentPages exist in initial importer. Geometry extractor stays pure. Repair must reuse same validation/persistence, support rollback-only dryrun without creating records.

- [ ] Step1: failing test using actual synthetic gridded PDF fixture. Import a source with column-major extracted text; assert original page text identical, pending candidate evidence_fk nonnull, sourcehash correct and exact cells/bboxes persisted. Replay repair inserts0; otherprofile sees0andcannotpersistownerbytes.

```python
before = page.extracted_text
report = persist_pdf_evidence(session, doc.id, profile_id=owner, pdf_bytes=pdf)
assert page.extracted_text == before
assert report.inserted > 0
assert all(row.status == ReviewStatus.NEEDS_REVIEW for row in new_rows)
```

- [ ] Step2: create immutable evidence ORM table with `(id,document_id,page_number)` uniqueness and composite FK to real pages, unique `(document_id,page_number,method,source_sha256)`; JSONB contains exact serialized row cells, method and source hash. LabObservation nullable `page_evidence_id` composite FK ensuring evidence belongs to same document and page. Add check/evidence-method version whitelist as appropriate without breaking historical view. Correction path must copy evidence ID. Never mutate existing evidence on conflict; compare exact content and reject mismatch. Use document lock for insert/replay and validate input SHA equals Document.sha256.
- [ ] Step3: parse each derived line with existing strict validator, convert to exact LabCandidate/observation without guessed conversions, dedup both existing flat and evidence rows across all statuses by complete source identity including name/value/unit/ref/flag/page. Per call max100pages,40emittedrows/page and150documents; bound reads25MiB each. Do not consume/reset worker cap or queue state. Existing per-page rows cap must not be circumvented silently: if legacy rows leave insufficient capacity, report blocked rather than reset; later explicit legacy repair will resolve cause.
- [ ] Step4: integrate new import after page persistence, before final report; combine flat and geometry candidates and processing counts, ensuring valid rows in formerlyunknown documents become laboratory_report/needs_review. Unsupported table extraction must not fail otherwisevalid import; record safe aggregate outcome without incorrectly claiming complete parsing. Do not clear integrity/conflicting-date errors. Repair selects only scoped PDFs inside FileVault.root, no symlink ancestors/targets, bounded regularfile descriptor read, hash verified; mismatch => blocked and no writes. Recheck ownership/hash under document lock after pure extraction. No filesystem/DB writes in dryrun.

```python
assert repair_pdf_evidence(session, vault, profile_id=owner, apply=False).inserted == 0
assert session.scalar(select(func.count(PageEvidence.id))) == previous
```

- [ ] Step5: cover import replay, repair replay after rejection/correction, competingrepair document lock, malformedhash/foreignprofile/symlink/outsidevault/oversize/no-page/unsupportedlayout, exactflags/references and no alias-basedidentitycollisions; legacy rows untouched; transaction rollback onfailedinsert; originalflat/OCR tests unchanged. Run newtests plus importer/schema/labs/geometry/Drive/Gmail integration suites, Ruff/mypy, append exact test evidence and commit. Root wires CLI and runs production repair after review.
