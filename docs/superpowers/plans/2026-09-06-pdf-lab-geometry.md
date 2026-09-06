# PDF lab geometry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Extract source-proven laboratory rows from supported PDF table geometry without changing existing flat text.

**Architecture:** Pure local adapter returns immutable row cells, bounding boxes and deterministic derived text. Initial implementation has no DB side effects; root's subsequent persistence task adds alternate evidence and importer/repair wiring.

**Tech Stack:** Installed PyMuPDF, current exact analyte/unit registry, Decimal, pytest synthetic PDF fixtures.

## Global Constraints

- User authorizes parallel development and read-only original inspection; no user check until development completes. Do not call clouds, modify archive/DB or read credentials in this task.
- Preserve every source field and cell coordinates. Whitespace may be normalized only in a separately labelled derived representation; originals stay untouched.
- No nearest-number guesses, prefix analyte matching, duplicate result-column guessing, inferred dates, physical unit conversion, or automatic verification.
- Every emitted row requires one exact header-role mapping and one non-overlapping geometry row. Unknown layouts/values remain unresolved.
- Limits: PDF≤50MiB,≤100pages;≤10tables/page,≤200bodyrows/table,≤8columns,≤500characters/cell,≤40emittedrows/page,≤60000derivedcharacters/page. Do not globally change existing queue limits.

### Task 1: Source-proven headered table extraction

**Files:** Create `src/health_agent/pdf_lab_geometry.py`, `tests/test_pdf_lab_geometry.py`, `docs/pdf-lab-geometry.md`. Narrow exact alias additions to `lab_extraction/registry.py` and tests allowed only for visibly unambiguous names/units stated below. No model/importer/queue/CLI/schema changes.

**Interfaces:** Export frozen `GeometryCell(text: str, bbox: tuple[float,float,float,float])`, `GeometryRow(name, result, unit, reference, comment: GeometryCell|None, derived_line: str)` and `GeometryPage(page_number:int, method:str, rows:tuple[GeometryRow,...], text:str, source_sha256:str)`; `extract_lab_geometry(pdf_bytes: bytes, page_number:int)->GeometryPage`. Fail invalid bytes/page/bounds with one safe ValueError code, no native diagnostics. Empty/unsupported pages return no rows. `method` is `pdf_table_v1`; source_sha256 hashes exact original bytes. Rows contain raw result cell including printed flag; raw values are not silently rewritten.

- [ ] Step 1: RED synthetic tests, actual PDF bytes drawn with PyMuPDF (test generation permitted). Prove column-major insertion order does not change correct mapping:

```python
result = extract_lab_geometry(synthetic_column_major_pdf, 1)
assert result.method == 'pdf_table_v1'
assert result.source_sha256 == hashlib.sha256(synthetic_column_major_pdf).hexdigest()
assert result.rows[0].name.text == 'Glucose'
assert result.rows[0].result.text == '5.10'
assert result.rows[0].unit.text == 'mmol/L'
assert result.rows[0].reference.text == '3.9-5.5'
assert result.rows[0].derived_line in result.text
```

Test duplicate result header, missing unit header, row spanning two result cells, intersecting/merged required cells, unknown analyte/unit, multiple numbers in result, two adjacent rows with different units, wrapped names, malformed/future dates not interpreted, foreign narrative numeric bands, unchanged input bytes and deterministic output. Header/value source insertion order deliberately differs from visual order. Use nonprivate invented data only.

- [ ] Step 2: Implement exact-role gridded table path using `page.find_tables()`, header labels `Исследование | Результат | Референсные значения | Ед. изм. | Коммент` or complete English equivalents `Test | Result | Reference range | Unit | Comment`. Each role exactly once, mapped by actual unique header cell. Every required body cell exists, has finite valid page-contained bounds, shares the geometry row, does not cross adjacent mapped column. Preserve comment but never treat its numbers as result. Reject invalid wholetable mapping before rows. Use raw cell text and bbox from extractor, not flattened column-index arithmetic detached from geometry.

- [ ] Step 3: Add explicit KDL four-column header path: exact complete header `Наименование исследования | Результат | Ед. измерения | Норм. значения`. Corresponding English `Test name | Result | Unit | Reference range` permitted for synthetic tests. Header words must form one unambiguous horizontal header band, each role once in increasing non-overlapping x intervals. Derive columns from those header bounds, not hardcoded page coordinates. Match result/unit/reference word boxes within each value row under the header; every cell has one row identity, with all raw words uniquely assigned. Name may wrap within the same row's vertical band but must never consume a neighbor. Reject if word boxes or multiple results make assignment ambiguous. No free y-band acceptance without this exact full header. Require nonempty source reference for both supported layouts. Subheading/footer words are not results. Stop at next header/footer or page bound; any overflow/ambiguous row is unresolved. KDL visible +/− flags may remain in raw result cell/derived evidence; they must not be discarded or silently mapped to H/L. This task can reject flagged rows if numeric proof cannot preserve flags yet; explicitly report rejected count.

- [ ] Step 4: Require exact registry analyte and compatible source unit for each accepted row. Preserve qualified tokens without changing their meaning. Whitespace-normalized source fields plus deterministic pipe row `name | value | unit | reference` may be the derived line only when exact numeric result token contains no extra flag; flagged rows remain raw structure for later source-flag support. Never omit a printed flag to force this format. Alias expansion permitted for `Средний объем эритроцитов (MCV)`, `Средняя концентрация Hb в эритроцитах (МСНС)`, `Среднее содержание гемоглобина в эритроците (МСН)`, `Средний объем тромбоцитов (MPV)`, and `пг/кл` as MCH pg only; exact full phrases, no substring matching. Additional aliases require root evidence/decision, not guessing.

- [ ] Step 5: Run `uv run pytest tests/test_pdf_lab_geometry.py tests/lab_extraction/test_registry.py -q` (resolve actual registry test path), Ruff changedfiles, mypy src and diff-check. After synthetic green, a bounded read-only ownerarchive dryrun may print aggregate counts only; no actual fields/paths in committed docs/tests. Root previously inspected KDL image at ignored data/medical-layout-34q37zcg/page.png. Commit and report supported layouts, accepted/rejected aggregate rows and implementation limitations; no persistence claims.
