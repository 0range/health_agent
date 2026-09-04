# Health Agent Core and Medical Import Implementation Plan

> **Status:** Reference detail only. Do not execute this document end-to-end; the authoritative scope is the lean v0.1 plan.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать локальную базу и безопасно превратить read-only Google Drive архив в проверяемую историю лабораторных показателей и отдельную Google-таблицу.

**Architecture:** Python-приложение рекурсивно читает настраиваемую Drive-папку, сохраняет неизменяемые копии и метаданные, извлекает текст из PDF, нормализует лабораторные строки и направляет неоднозначности в review queue. PostgreSQL является источником правды; Sheets — отдельная витрина и интерфейс ручного подтверждения.

**Tech Stack:** Python 3.12, uv, PostgreSQL 16, SQLAlchemy 2.0.52, Alembic 1.19.1, Pydantic Settings 2.15.0, Google API Client 2.200.0, google-auth-oauthlib 1.4.1, PyMuPDF 1.28.2, pdfplumber 0.11.10, OCRmyPDF/Tesseract, pytest 9.1.1, Ruff 0.16.6, mypy 2.3.1.

## Global Constraints

- Все файлы находятся внутри `health-agent/`.
- Drive source использует только `drive.readonly`; Sheets publisher использует отдельный OAuth profile.
- Реальные PDF, OAuth credentials, токены, выгрузки и база исключены из Git.
- Входящие Drive-файлы никогда не изменяются.
- Сомнительные результаты имеют статус `needs_review` и исключены из опубликованных графиков.
- Дата забора материала и дата документа хранятся отдельно.
- Исходные названия, единицы и референсы сохраняются рядом с нормализованными.

---

## File map

- `pyproject.toml` — зависимости, CLI entry point и настройки инструментов.
- `compose.yaml` — PostgreSQL; Metabase добавляется третьим планом.
- `src/health_hub/config.py` — не секретная конфигурация из окружения.
- `src/health_hub/security/local_secrets.py` — атомарные локальные token/key files с правами `0600`.
- `src/health_hub/db/models.py` — SQLAlchemy schema.
- `src/health_hub/db/repositories.py` — идемпотентные записи и review transitions.
- `src/health_hub/drive/client.py` — read-only recursive listing/download.
- `src/health_hub/documents/text.py` — digital text и OCR fallback.
- `src/health_hub/documents/classifier.py` — content-based document type.
- `src/health_hub/labs/extractor.py` — строки лабораторных результатов.
- `src/health_hub/labs/normalizer.py` — aliases и unit conversion.
- `src/health_hub/sheets/publisher.py` — четыре листа и review round-trip.
- `src/health_hub/sync/medical.py` — единая транзакционная orchestration.
- `src/health_hub/cli.py` — `db`, `auth`, `sync medical`, `publish sheets`.
- `tests/fixtures/` — только синтетические PDF и API payloads.

### Task 1: Bootstrap the isolated project and local PostgreSQL

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `compose.yaml`
- Create: `src/health_hub/__init__.py`
- Create: `src/health_hub/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: CLI command `health-hub --help`; PostgreSQL at `127.0.0.1:55432`.

- [ ] **Step 1: Initialize Git and write the CLI smoke test**

```bash
cd /Users/vitali.arz/Applications/VibeCoding/health-agent
git init
uv init --bare --python 3.12
mkdir -p src/health_hub tests
uv add --dev pytest==9.1.1
```

```python
# tests/test_cli.py
from health_hub.cli import main

def test_help_exits_cleanly(monkeypatch):
    monkeypatch.setattr("sys.argv", ["health-hub", "--help"])
    assert main() == 0
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `uv run pytest tests/test_cli.py -q`

Expected: FAIL because `health_hub.cli` does not exist.

- [ ] **Step 3: Add pinned dependencies, CLI, ignores and Compose**

```toml
# pyproject.toml excerpts
[project]
name = "personal-health-hub"
requires-python = ">=3.12,<3.14"
dependencies = [
  "alembic==1.19.1", "google-api-python-client==2.200.0",
  "google-auth-oauthlib==1.4.1",
  "pdfplumber==0.11.10", "psycopg[binary]==3.2.10",
  "pydantic-settings==2.15.0", "pymupdf==1.28.2",
  "sqlalchemy==2.0.52", "tenacity==9.1.4"
]
[project.scripts]
health-hub = "health_hub.cli:main"
[dependency-groups]
dev = ["mypy==2.3.1", "pip-audit==2.9.0", "pytest==9.1.1", "ruff==0.16.6"]
```

```python
# src/health_hub/cli.py
import argparse

def main() -> int:
    parser = argparse.ArgumentParser(prog="health-hub")
    parser.add_argument("command", nargs="?")
    parser.parse_args()
    return 0
```

```yaml
# compose.yaml
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    ports: ["127.0.0.1:55432:5432"]
    environment:
      POSTGRES_DB: health_hub
      POSTGRES_USER: health_hub
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: ["health_pg:/var/lib/postgresql/data"]
volumes:
  health_pg: {}
```

`.gitignore` must include `.env`, `client_secret*.json`, `data/`, `.tokens/`, `*.pdf`, `*.db`, `*.sql.gz`, and `.DS_Store`. `.env.example` contains names with empty values: `DATABASE_URL`, `POSTGRES_PASSWORD`, `HEALTH_DRIVE_ROOT_FOLDER_ID`, `GOOGLE_OAUTH_CLIENT_FILE`.

- [ ] **Step 4: Verify bootstrap and security gates**

Run: `uv sync && uv run pytest -q && uv run ruff check . && uv run mypy src && uv run pip-audit`

Expected: all commands exit 0; audit reports no known vulnerable locked dependency.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add .gitignore .env.example pyproject.toml uv.lock compose.yaml src tests
git commit -m "build: bootstrap local health hub"
```

### Task 2: Define the provenance-first database schema

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_core_medical.py`
- Create: `src/health_hub/db/base.py`
- Create: `src/health_hub/db/models.py`
- Create: `src/health_hub/db/repositories.py`
- Test: `tests/db/test_medical_schema.py`

**Interfaces:**
- Produces: `upsert_document(session, DriveFile) -> Document`; `save_lab_result(session, LabResultDraft) -> LabResult`; `resolve_review(session, review_id, correction) -> LabResult`.

- [ ] **Step 1: Write schema invariants as failing tests**

```python
def test_document_drive_id_and_revision_are_unique(session):
    add_document(session, drive_id="f1", revision="md5-a")
    with pytest.raises(IntegrityError):
        add_document(session, drive_id="f1", revision="md5-a")

def test_unverified_result_is_not_publishable(session):
    result = make_lab_result(session, status="needs_review")
    assert result.is_publishable is False
```

- [ ] **Step 2: Run migration tests and confirm failure**

Run: `uv run pytest tests/db/test_medical_schema.py -q`

Expected: FAIL because models and migrations do not exist.

- [ ] **Step 3: Implement explicit tables and enums**

```python
class ReviewStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    drive_file_id: Mapped[str]
    drive_revision: Mapped[str]
    source_url: Mapped[str]
    sha256: Mapped[str]
    document_type: Mapped[str]
    specimen_collected_at: Mapped[datetime | None]
    issued_at: Mapped[datetime | None]
    __table_args__ = (UniqueConstraint("drive_file_id", "drive_revision"),)
```

Add `source_connections`, `sync_runs`, `raw_records`, `documents`, `document_pages`, `lab_tests`, `lab_aliases`, `lab_results`, `review_items`, and `audit_events` with these cross-plan fields:

- `source_connections(id, provider, external_account_id, auth_status, granted_scopes, capabilities, sync_cursors, last_success_at, expected_interval_seconds)`; the initial row uses provider `google_drive` and later providers reuse the table;
- `sync_runs(id, connection_id, mode, status, requested_from, requested_to, pages_fetched, raw_created, normalized_created, normalized_updated, unchanged, failed, safe_error_code, safe_error_message, started_at, completed_at)`;
- `raw_records(connection_id, resource_kind, external_id, source_updated_at, payload_sha256, payload, status)` with append-only revisions;
- `documents(id, drive_file_id, drive_revision, source_url, sha256, document_type, laboratory_name, specimen_collected_at, issued_at)`;
- `lab_tests(id, canonical_code, display_name, category)` with unique `canonical_code`;
- `lab_results(id, lab_test_id, document_id, collected_at, source_value, source_unit, normalized_value, normalized_unit, reference_low, reference_high, reference_text, page_number, evidence_excerpt, confidence, status, superseded_result_id)`.

`review_items` links to the proposed result and stores reason codes, decision, correction payload and timestamps. `audit_events` records every verification, correction and rejection without token or medical-text payloads.

- [ ] **Step 4: Apply migration twice and run tests**

Run: `uv run alembic upgrade head && uv run alembic upgrade head && uv run pytest tests/db -q`

Expected: second migration is a no-op and tests pass.

- [ ] **Step 5: Commit schema**

```bash
git add alembic.ini alembic src/health_hub/db tests/db
git commit -m "feat: add provenance-first medical schema"
```

### Task 3: Store credentials in protected local files

**Files:**
- Create: `src/health_hub/config.py`
- Create: `src/health_hub/security/local_secrets.py`
- Test: `tests/security/test_local_secrets.py`

**Interfaces:**
- Produces: `LocalSecretStore.get(name: str) -> str | None`; `LocalSecretStore.set(name: str, value: str) -> None`; `Settings`.

- [ ] **Step 1: Write tests for permissions and round-trip**

```python
def test_secret_round_trip(tmp_path):
    store = LocalSecretStore(root=tmp_path)
    store.set("google-drive-token", '{"token":"redacted"}')
    assert store.get("google-drive-token") == '{"token":"redacted"}'
    assert stat.S_IMODE((tmp_path / "google-drive-token.json").stat().st_mode) == 0o600
    assert "redacted" not in repr(store)
```

- [ ] **Step 2: Verify failure before implementation**

Run: `uv run pytest tests/security/test_local_secrets.py -q`

Expected: FAIL with missing `LocalSecretStore`.

- [ ] **Step 3: Implement atomic local secret files**

```python
@dataclass(repr=False)
class LocalSecretStore:
    root: Path

    def get(self, name: str) -> str | None:
        path = self.root / f"{name}.json"
        return path.read_text() if path.exists() else None

    def set(self, name: str, value: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / f"{name}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value)
        tmp.chmod(0o600)
        os.replace(tmp, path)
```

Store tokens under `data/secrets/`, which is excluded from Git. Add a log filter that redacts fields matching `token`, `secret`, `password`, and `authorization`.

- [ ] **Step 4: Run security tests and scan tracked files**

Run: `uv run pytest tests/security -q && ! git grep -Ei '(access_token|refresh_token|client_secret).{0,20}[=:].{0,5}[A-Za-z0-9_-]{12}'`

Expected: tests pass and the secret scan returns no match.

- [ ] **Step 5: Commit credential storage**

```bash
git add src/health_hub/config.py src/health_hub/security tests/security
git commit -m "feat: protect local connector token files"
```

### Task 4: Implement the read-only Google Drive source

**Files:**
- Create: `src/health_hub/drive/types.py`
- Create: `src/health_hub/drive/client.py`
- Create: `src/health_hub/drive/auth.py`
- Test: `tests/drive/test_client.py`

**Interfaces:**
- Produces: `DriveSource.iter_files(root_id: str) -> Iterator[DriveFile]`; `DriveSource.download(file, target) -> Path`.

- [ ] **Step 1: Test recursion without relying on folder names**

```python
def test_iter_files_recurses_and_returns_only_supported_files(fake_drive):
    fake_drive.tree({"root": [folder("year-x"), pdf("p1")], "year-x": [pdf("p2"), image("i1")]})
    files = list(DriveSource(fake_drive).iter_files("root"))
    assert [f.id for f in files] == ["p1", "p2", "i1"]
    assert fake_drive.write_calls == []
```

- [ ] **Step 2: Confirm tests fail before the client exists**

Run: `uv run pytest tests/drive/test_client.py -q`

Expected: FAIL with missing `DriveSource`.

- [ ] **Step 3: Implement paginated read-only traversal and streaming download**

```python
class DriveSource:
    def iter_files(self, root_id: str) -> Iterator[DriveFile]:
        stack = [root_id]
        while stack:
            parent = stack.pop()
            for item in self._list_all(parent):
                if item.mime_type == FOLDER_MIME:
                    stack.append(item.id)
                elif item.mime_type in SUPPORTED_MIME_TYPES:
                    yield item
```

OAuth request must use only `https://www.googleapis.com/auth/drive.readonly`. Download into `data/source-cache/<drive-id>/<revision>/original.<ext>` using a temporary file, verify size/hash, then atomically rename.

- [ ] **Step 4: Run unit tests and a non-downloading inventory command**

Run: `uv run pytest tests/drive -q && uv run health-hub drive inventory --no-download`

Expected: tests pass; local authenticated run reports 122 current PDFs and performs no Drive writes.

- [ ] **Step 5: Commit Drive source**

```bash
git add src/health_hub/drive tests/drive
git commit -m "feat: add read-only recursive drive ingestion"
```

### Task 5: Extract text and classify documents locally

**Files:**
- Create: `src/health_hub/documents/text.py`
- Create: `src/health_hub/documents/classifier.py`
- Create: `src/health_hub/documents/types.py`
- Test: `tests/documents/test_text.py`
- Test: `tests/documents/test_classifier.py`
- Create: `tests/fixtures/lab-digital.pdf`
- Create: `tests/fixtures/lab-scan.pdf`

**Interfaces:**
- Produces: `extract_document(path: Path) -> ExtractedDocument`; `classify_document(doc) -> DocumentKind`.

- [ ] **Step 1: Generate synthetic fixtures and failing tests**

```python
def test_digital_pdf_keeps_page_provenance():
    doc = extract_document(FIXTURES / "lab-digital.pdf")
    assert doc.pages[0].number == 1
    assert "Гемоглобин" in doc.pages[0].text

def test_classifier_uses_content_not_parent_folder():
    doc = ExtractedDocument.synthetic("МРТ поясничного отдела")
    assert classify_document(doc) is DocumentKind.IMAGING
```

Create fixtures from synthetic values during the test build; do not copy user PDFs.

- [ ] **Step 2: Run tests and observe missing implementation**

Run: `uv run pytest tests/documents -q`

Expected: FAIL because extraction functions are absent.

- [ ] **Step 3: Implement digital-first extraction with OCR fallback**

```python
def extract_document(path: Path) -> ExtractedDocument:
    digital = extract_with_pymupdf(path)
    if digital.character_count >= 80 and digital.text_page_ratio >= 0.6:
        return digital
    ocr_path = run_ocrmypdf(path, languages="rus+eng")
    return extract_with_pymupdf(ocr_path)
```

Install local OCR prerequisites with `brew install ocrmypdf tesseract-lang`. Classify into `laboratory`, `imaging`, `ultrasound`, `cardiology`, `consultation`, `endoscopy_histology`, `checkup`, or `unknown`. Record which extraction method ran and its errors.

- [ ] **Step 4: Test digital, scanned and corrupt documents**

Run: `uv run pytest tests/documents -q`

Expected: digital and OCR fixtures pass; corrupt fixture returns a recorded processing error rather than aborting the batch.

- [ ] **Step 5: Commit document pipeline**

```bash
git add src/health_hub/documents tests/documents tests/fixtures
git commit -m "feat: extract and classify medical pdfs locally"
```

### Task 6: Parse, normalize and review laboratory results

**Files:**
- Create: `src/health_hub/labs/types.py`
- Create: `src/health_hub/labs/extractor.py`
- Create: `src/health_hub/labs/normalizer.py`
- Create: `src/health_hub/labs/catalog.csv`
- Test: `tests/labs/test_extractor.py`
- Test: `tests/labs/test_normalizer.py`
- Test: `tests/labs/test_review_policy.py`

**Interfaces:**
- Produces: `extract_lab_results(doc) -> list[LabResultDraft]`; `normalize_result(draft, catalog) -> NormalizedLabResult`; `review_status(result) -> ReviewStatus`.

- [ ] **Step 1: Encode representative synthetic rows**

```python
@pytest.mark.parametrize("line,name,value,unit", [
    ("Ферритин 42.0 нг/мл 30–400", "ferritin", Decimal("42.0"), "ng/mL"),
    ("25-OH Vitamin D 28 ng/ml 30 - 100", "vitamin_d_25_oh", Decimal("28"), "ng/mL"),
    ("Гемоглобин 145 г/л 130–170", "hemoglobin", Decimal("145"), "g/L"),
])
def test_parse_row(line, name, value, unit):
    result = parse_lab_line(line)
    assert (result.test_code, result.value, result.unit) == (name, value, unit)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/labs -q`

Expected: FAIL with missing parser and catalog.

- [ ] **Step 3: Implement conservative parsing and review policy**

```python
def review_status(result: NormalizedLabResult) -> ReviewStatus:
    required = [result.test_code, result.value, result.source_unit, result.collected_at, result.page]
    if any(item is None for item in required):
        return ReviewStatus.NEEDS_REVIEW
    if result.confidence < Decimal("0.92") or result.unit_conversion_warning:
        return ReviewStatus.NEEDS_REVIEW
    return ReviewStatus.VERIFIED
```

Seed canonical codes and aliases for CBC, iron, lipids, glucose/HbA1c/insulin, vitamins, thyroid/hormones, liver, kidney and inflammation. Conversion functions use `Decimal`; unsupported conversions stay unchanged and enter review. Never infer a missing reference range.

- [ ] **Step 4: Run tests including locale and unit cases**

Run: `uv run pytest tests/labs -q`

Expected: all synthetic Russian/English decimal, range and unit cases pass; ambiguous rows become `needs_review`.

- [ ] **Step 5: Commit lab pipeline**

```bash
git add src/health_hub/labs tests/labs
git commit -m "feat: normalize labs with manual review policy"
```

### Task 7: Publish a separate Google Sheet and process corrections

**Files:**
- Create: `src/health_hub/sheets/auth.py`
- Create: `src/health_hub/sheets/publisher.py`
- Create: `src/health_hub/sheets/schema.py`
- Test: `tests/sheets/test_publisher.py`
- Test: `tests/sheets/test_review_import.py`

**Interfaces:**
- Produces: `publish_workbook(session, sheets_client, spreadsheet_id) -> PublishReport`; `apply_review_rows(session, rows) -> ReviewReport`.

- [ ] **Step 1: Test the four-sheet contract**

```python
def test_publish_contract(fake_sheets, seeded_session):
    report = publish_workbook(seeded_session, fake_sheets, "sheet-1")
    assert fake_sheets.tabs == ["Ключевые показатели", "История анализов", "Требует проверки", "Источники"]
    assert report.review_rows == 1
    assert "source_url" in fake_sheets.header("История анализов")
```

- [ ] **Step 2: Confirm failure before publisher implementation**

Run: `uv run pytest tests/sheets -q`

Expected: FAIL with missing publisher.

- [ ] **Step 3: Implement idempotent batch publishing and reviewed corrections**

```python
SHEET_COLUMNS = {
    "История анализов": ["result_id", "date", "test", "value", "unit", "reference", "status", "source_url"],
    "Требует проверки": ["review_id", "source_url", "page", "excerpt", "recognized_value", "corrected_value", "corrected_unit", "decision"],
}
```

Create the workbook outside the source archive using `spreadsheets` plus `drive.file` scopes. Store its non-secret ID in local configuration. Publisher replaces app-owned ranges in batches. Review importer accepts only `confirm`, `correct`, or `reject`, validates values, creates an `audit_event`, and never edits the source document.

- [ ] **Step 4: Run publisher and round-trip tests**

Run: `uv run pytest tests/sheets -q`

Expected: repeated publish yields identical rows; correction creates a new verified version and preserves the original.

- [ ] **Step 5: Commit Sheets integration**

```bash
git add src/health_hub/sheets tests/sheets
git commit -m "feat: publish labs and process manual review in sheets"
```

### Task 8: Orchestrate the first medical backfill safely

**Files:**
- Create: `src/health_hub/sync/medical.py`
- Modify: `src/health_hub/cli.py`
- Create: `tests/sync/test_medical_sync.py`
- Create: `docs/runbooks/medical-import.md`

**Interfaces:**
- Produces: `sync_medical(session, source, extractor) -> SyncReport`; CLI `health-hub sync medical --dry-run|--apply`.

- [ ] **Step 1: Test idempotency and source isolation**

```python
def test_same_inventory_twice_creates_no_duplicates(stack):
    first = stack.sync_medical()
    second = stack.sync_medical()
    assert first.created_documents == 3
    assert second.created_documents == 0
    assert stack.drive.write_calls == []
```

- [ ] **Step 2: Run the sync test and confirm failure**

Run: `uv run pytest tests/sync/test_medical_sync.py -q`

Expected: FAIL with missing orchestration.

- [ ] **Step 3: Implement per-document transactions and resume**

```python
def sync_medical(session_factory, source, processor) -> SyncReport:
    run = start_sync_run("google_drive_medical")
    for file in source.iter_files(settings.health_drive_root_folder_id):
        with session_factory.begin() as session:
            process_if_new_revision(session, file, processor)
    return finish_sync_run(run)
```

`--dry-run` lists counts only. `--apply` downloads and processes. A failed PDF records an error and continues. The runbook includes OAuth setup, dry run, apply, review, republish, safe restart and rollback instructions.

- [ ] **Step 4: Run synthetic E2E and real dry run**

Run: `uv run pytest -q && uv run health-hub sync medical --dry-run`

Expected: all tests pass; authenticated dry run reports the current Drive inventory without writes or downloads.

- [ ] **Step 5: Execute the controlled real backfill and inspect counts**

Run: `uv run health-hub sync medical --apply && uv run health-hub publish sheets`

Expected: 122 current PDFs are accounted for as processed, queued, duplicate, or failed; every failure has a safe reason; Drive write count is zero.

- [ ] **Step 6: Commit orchestration and runbook**

```bash
git add src/health_hub/sync src/health_hub/cli.py tests/sync docs/runbooks/medical-import.md
git commit -m "feat: orchestrate resumable medical archive backfill"
```
