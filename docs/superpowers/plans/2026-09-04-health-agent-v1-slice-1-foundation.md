# Health Agent v1 Slice 1: Local Foundation and First Medical Document Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **TL;DR:** поднять локальный PostgreSQL и Metabase, импортировать один PDF без дублей, сохранить происхождение, исключить сомнительные значения из графиков и показать проверенную динамику.

**Goal:** Build the smallest real vertical slice in which a medical PDF becomes provenance-backed laboratory data and a local Metabase chart.

**Architecture:** A Python CLI writes immutable originals and normalized records to local PostgreSQL. A conservative PDF extractor creates verified or review-required lab observations; only verified observations enter a SQL view used by Metabase. Containers bind only to localhost and application code is independent from later Drive, Gmail, WHOOP and Telegram adapters.

**Tech Stack:** Python 3.13 managed by uv, PostgreSQL 18.6, SQLAlchemy 2, Alembic, Pydantic Settings, PyMuPDF, Typer, pytest, Ruff, mypy, Colima/Docker Compose, Metabase OSS 0.63.13.

## Global Constraints

- All project files live inside `/Users/vitali.arz/Applications/VibeCoding/health-agent`.
- Real medical documents, extracted text, database volumes, credentials and tokens never enter Git.
- PostgreSQL and Metabase listen only on `127.0.0.1`.
- An original file is content-addressed by SHA-256 and never overwritten.
- Every normalized value retains source document, page, evidence text, original value/unit, confidence and review status.
- `needs_review` and `rejected` observations never appear in dashboard views or agent answers.
- Re-importing the same bytes is a no-op with an explicit `duplicate` result.
- Routine failures produce safe error codes without medical text in logs.

---

## File map

- `pyproject.toml` — package, CLI and locked Python dependencies.
- `compose.yaml` — localhost-only PostgreSQL, Metabase and their persistent volumes.
- `.env.example` / `.gitignore` — non-secret contract and exclusions.
- `docker/postgres/init/001-metabase.sql` — separate Metabase application database in the same local PostgreSQL service.
- `src/health_agent/config.py` — validated paths and connection settings.
- `src/health_agent/db.py` — engine and transaction boundary.
- `src/health_agent/models.py` — source, document, page, lab observation and review models.
- `alembic/` — reproducible schema and verified-only dashboard view.
- `src/health_agent/vault.py` — immutable content-addressed file storage.
- `src/health_agent/pdf.py` — PDF text extraction with page coordinates.
- `src/health_agent/labs.py` — conservative lab-row candidates and status rules.
- `src/health_agent/importer.py` — idempotent orchestration.
- `src/health_agent/cli.py` — setup, status, import and review commands.
- `src/health_agent/metabase.py` — idempotent local Metabase bootstrap.
- `tests/` — synthetic documents and isolated unit/integration tests only.

### Task 1: Bootstrap a runnable local project

**Files:**
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `compose.yaml`
- Create: `docker/postgres/init/001-metabase.sql`
- Create: `src/health_agent/__init__.py`
- Create: `src/health_agent/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: shell command `health-agent --help`; services `postgres` and `metabase`; localhost ports `55432` and `53000`.

- [ ] **Step 1: Initialize Git and dependency metadata**

Run:

```bash
cd /Users/vitali.arz/Applications/VibeCoding/health-agent
git init
uv python install 3.13
uv init --bare --python 3.13
```

- [ ] **Step 2: Write the failing CLI smoke test**

```python
from typer.testing import CliRunner

from health_agent.cli import app


def test_help_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Personal Health Agent" in result.stdout
```

Run: `uv run pytest tests/test_cli.py -q`

Expected: FAIL because `health_agent.cli` does not exist.

- [ ] **Step 3: Add dependencies and the minimal CLI**

Run:

```bash
uv add alembic "psycopg[binary]" pydantic-settings pymupdf sqlalchemy tenacity typer httpx
uv add --dev mypy pytest ruff
```

```python
import typer

app = typer.Typer(help="Personal Health Agent")


def main() -> None:
    app()
```

`pyproject.toml` exposes `health-agent = "health_agent.cli:main"`. `.gitignore` includes `.env`, `.tokens/`, `data/`, `*.pdf`, `*.png`, `*.jpg`, `*.sql`, `*.dump`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/` and `.DS_Store`.

- [ ] **Step 4: Add the local services**

`compose.yaml` defines:

```yaml
services:
  postgres:
    image: postgres:18.6-alpine
    restart: unless-stopped
    ports: ["127.0.0.1:55432:5432"]
    environment:
      POSTGRES_DB: health_agent
      POSTGRES_USER: health_agent
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U health_agent -d health_agent"]
      interval: 5s
      timeout: 3s
      retries: 20
    volumes: ["health_postgres:/var/lib/postgresql/data"]
  metabase:
    image: metabase/metabase:v0.63.13
    restart: unless-stopped
    ports: ["127.0.0.1:53000:3000"]
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      MB_DB_TYPE: postgres
      MB_DB_HOST: postgres
      MB_DB_PORT: 5432
      MB_DB_DBNAME: metabase
      MB_DB_USER: health_agent
      MB_DB_PASS: ${POSTGRES_PASSWORD}
    volumes: ["health_metabase:/metabase-data"]
```

The PostgreSQL service additionally mounts the initialization directory:

```yaml
    volumes:
      - health_postgres:/var/lib/postgresql/data
      - ./docker/postgres/init:/docker-entrypoint-initdb.d:ro
```

The initialization SQL is intentionally minimal:

```sql
CREATE DATABASE metabase;
```

The two applications use separate databases in one localhost-only PostgreSQL service. A separate database role is unnecessary for this single-user local version.

Complete the volume declarations:

```yaml
volumes:
  health_postgres: {}
  health_metabase: {}
```

- [ ] **Step 5: Verify the project boundary**

Run: `uv sync && uv run pytest tests/test_cli.py -q && uv run ruff check .`

Expected: PASS; `git status --short` lists no `.env` or `data/` contents.

- [ ] **Step 6: Commit**

```bash
git add .python-version .gitignore .env.example pyproject.toml uv.lock compose.yaml docker src tests
git commit -m "build: bootstrap local health agent"
```

### Task 2: Create the provenance-first schema

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_medical_core.py`
- Create: `src/health_agent/config.py`
- Create: `src/health_agent/db.py`
- Create: `src/health_agent/models.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces: `Settings`, `build_engine(settings)`, `session_scope(engine)`, SQLAlchemy models `SourceRecord`, `Document`, `DocumentPage`, `LabObservation`, `ReviewItem`.

- [ ] **Step 1: Write schema-invariant tests**

```python
def test_same_source_revision_is_unique(session):
    session.add(SourceRecord(provider="local_file", external_id="abc", revision="sha256:1"))
    session.commit()
    session.add(SourceRecord(provider="local_file", external_id="abc", revision="sha256:1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_review_required_observation_is_not_publishable(session):
    observation = make_observation(session, status="needs_review")
    assert observation.is_publishable is False
```

Run: `uv run pytest tests/test_schema.py -q`

Expected: FAIL because the schema does not exist.

- [ ] **Step 2: Implement explicit models and enums**

```python
class ReviewStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class SourceRecord(Base):
    __tablename__ = "source_records"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str]
    external_id: Mapped[str]
    revision: Mapped[str]
    source_uri: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(default=utc_now)
    __table_args__ = (UniqueConstraint("provider", "external_id", "revision"),)
```

Add the following required columns:

- `documents(source_record_id, sha256, vault_path, media_type, document_type, issued_at, collected_at, processing_status, safe_error_code)`;
- `document_pages(document_id, page_number, extracted_text, extraction_method)`;
- `lab_observations(document_id, page_number, canonical_name, source_name, source_value, source_unit, normalized_value, normalized_unit, reference_low, reference_high, reference_text, evidence_excerpt, confidence, status)`;
- `review_items(observation_id, reason_code, decision, correction_json, created_at, resolved_at)`.

Create SQL view `verified_lab_history` that selects only `lab_observations.status = 'verified'`.

- [ ] **Step 3: Generate and apply the migration**

Run:

```bash
uv run alembic revision --autogenerate -m medical_core
uv run alembic upgrade head
uv run alembic upgrade head
```

Expected: first command creates the schema; the second upgrade is a no-op.

- [ ] **Step 4: Run schema tests**

Run: `uv run pytest tests/test_schema.py -q`

Expected: PASS against the local PostgreSQL test database.

- [ ] **Step 5: Commit**

```bash
git add alembic.ini alembic src/health_agent/config.py src/health_agent/db.py src/health_agent/models.py tests/test_schema.py
git commit -m "feat: add provenance-first medical schema"
```

### Task 3: Store originals idempotently

**Files:**
- Create: `src/health_agent/vault.py`
- Test: `tests/test_vault.py`

**Interfaces:**
- Produces: `StoredFile(sha256: str, path: Path, size_bytes: int)` and `FileVault.store(source: Path) -> StoredFile`.

- [ ] **Step 1: Write failing immutability tests**

```python
def test_same_bytes_have_one_vault_object(tmp_path):
    source_a = tmp_path / "a.pdf"
    source_b = tmp_path / "b.pdf"
    source_a.write_bytes(b"same")
    source_b.write_bytes(b"same")
    vault = FileVault(tmp_path / "vault")
    first = vault.store(source_a)
    second = vault.store(source_b)
    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert first.path.read_bytes() == b"same"
```

Run: `uv run pytest tests/test_vault.py -q`

Expected: FAIL because `FileVault` does not exist.

- [ ] **Step 2: Implement atomic content-addressed storage**

```python
class FileVault:
    def store(self, source: Path) -> StoredFile:
        digest = sha256_file(source)
        target = self.root / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".partial")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
            target.chmod(0o600)
        return StoredFile(digest, target, target.stat().st_size)
```

Verify an existing target's hash before returning it; raise `VaultIntegrityError` on mismatch without overwriting either file.

- [ ] **Step 3: Verify and commit**

Run: `uv run pytest tests/test_vault.py -q`

Expected: PASS.

```bash
git add src/health_agent/vault.py tests/test_vault.py
git commit -m "feat: add immutable medical file vault"
```

### Task 4: Extract page text and conservative laboratory candidates

**Files:**
- Create: `src/health_agent/pdf.py`
- Create: `src/health_agent/labs.py`
- Test: `tests/test_pdf.py`
- Test: `tests/test_labs.py`

**Interfaces:**
- Produces: `extract_pdf(path: Path) -> ExtractedPdf`; `parse_lab_candidates(pages: tuple[ExtractedPage, ...]) -> tuple[LabCandidate, ...]`.

- [ ] **Step 1: Write extraction and parsing tests**

```python
def test_extract_pdf_preserves_page_number(synthetic_lab_pdf):
    result = extract_pdf(synthetic_lab_pdf)
    assert result.pages[0].page_number == 1
    assert "Ферритин" in result.pages[0].text


def test_parser_preserves_source_and_marks_ambiguous_rows():
    pages = (ExtractedPage(1, "Ферритин 42 нг/мл 30-400"),)
    candidate = parse_lab_candidates(pages)[0]
    assert candidate.source_name == "Ферритин"
    assert candidate.source_value == "42"
    assert candidate.reference_text == "30-400"
    assert candidate.status == ReviewStatus.NEEDS_REVIEW
```

Run: `uv run pytest tests/test_pdf.py tests/test_labs.py -q`

Expected: FAIL because extraction modules do not exist.

- [ ] **Step 2: Implement digital-text extraction**

```python
def extract_pdf(path: Path) -> ExtractedPdf:
    with pymupdf.open(path) as document:
        pages = tuple(
            ExtractedPage(page_number=index + 1, text=page.get_text("text"))
            for index, page in enumerate(document)
        )
    method = "digital_text" if any(page.text.strip() for page in pages) else "ocr_required"
    return ExtractedPdf(pages=pages, extraction_method=method)
```

Empty/scanned pages receive `ocr_required`; no value is guessed from them. OCR is added only after inspecting the first real document that requires it.

- [ ] **Step 3: Implement a conservative row parser**

Use `Decimal` for values. Preserve the complete source line as `evidence_excerpt`. Recognize a numeric value only when name, value and unit occur on one line; recognize a reference interval only when both limits parse unambiguously. Known aliases initially cover ferritin, B12, folate/B9, total/LDL/HDL cholesterol, triglycerides, iron, vitamin D and prolactin. Every candidate starts `needs_review`; only explicit review can verify it in Slice 1.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/test_pdf.py tests/test_labs.py -q`

Expected: PASS.

```bash
git add src/health_agent/pdf.py src/health_agent/labs.py tests/test_pdf.py tests/test_labs.py
git commit -m "feat: extract conservative lab candidates from pdf"
```

### Task 5: Orchestrate import, duplicate detection and review

**Files:**
- Create: `src/health_agent/importer.py`
- Modify: `src/health_agent/cli.py`
- Test: `tests/test_importer.py`
- Test: `tests/test_review_cli.py`

**Interfaces:**
- Produces: `import_document(session, vault, source_path, source_uri) -> ImportReport`; CLI commands `import-file`, `review list`, `review approve`, `review reject`.

- [ ] **Step 1: Write failing end-to-end service tests**

```python
def test_reimport_is_duplicate(session, vault, synthetic_lab_pdf):
    first = import_document(session, vault, synthetic_lab_pdf, "local:test")
    second = import_document(session, vault, synthetic_lab_pdf, "local:test")
    assert first.status == "imported"
    assert second.status == "duplicate"
    assert second.document_id == first.document_id


def test_approval_moves_value_into_verified_view(session, imported_candidate):
    approve_observation(session, imported_candidate.id)
    rows = session.execute(text("select * from verified_lab_history")).all()
    assert [row.canonical_name for row in rows] == ["ferritin"]
```

Run: `uv run pytest tests/test_importer.py tests/test_review_cli.py -q`

Expected: FAIL because orchestration does not exist.

- [ ] **Step 2: Implement one transaction per document**

`import_document` must:

1. store bytes in the vault;
2. return the existing document when SHA-256 already exists;
3. create `SourceRecord` and `Document`;
4. extract and store pages;
5. create candidates and matching `ReviewItem` rows;
6. commit all records together or roll back all database changes;
7. return counts without including medical text.

- [ ] **Step 3: Implement explicit review transitions**

```python
def approve_observation(session: Session, observation_id: UUID) -> None:
    observation = session.get_one(LabObservation, observation_id)
    if observation.status is not ReviewStatus.NEEDS_REVIEW:
        raise InvalidReviewTransition(observation.status)
    observation.status = ReviewStatus.VERIFIED
    observation.review_item.decision = "approved"
    observation.review_item.resolved_at = utc_now()
```

Reject follows the same one-way rule. A correction creates a new verified observation and links the original as superseded; it never mutates the source evidence.

- [ ] **Step 4: Add human-readable CLI output**

`health-agent import-file PATH` prints only status, document ID and candidate/review counts. `health-agent review list` prints observation ID, source name/value/unit, page and source filename. Approval requires the observation UUID.

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/test_importer.py tests/test_review_cli.py -q`

Expected: PASS.

```bash
git add src/health_agent/importer.py src/health_agent/cli.py tests/test_importer.py tests/test_review_cli.py
git commit -m "feat: import and review medical documents"
```

### Task 6: Bootstrap the first Metabase graph

**Files:**
- Create: `src/health_agent/metabase.py`
- Modify: `src/health_agent/cli.py`
- Test: `tests/test_metabase.py`

**Interfaces:**
- Produces: `bootstrap_metabase(settings) -> MetabaseBootstrapResult`; CLI command `health-agent dashboard setup`.

- [ ] **Step 1: Write a failing idempotency test with mocked HTTP**

```python
def test_bootstrap_reuses_existing_collection_and_card(fake_metabase, settings):
    first = bootstrap_metabase(settings)
    second = bootstrap_metabase(settings)
    assert first.dashboard_id == second.dashboard_id
    assert fake_metabase.count_named("Анализы крови") == 1
```

Run: `uv run pytest tests/test_metabase.py -q`

Expected: FAIL because the bootstrap client does not exist.

- [ ] **Step 2: Implement idempotent Metabase setup**

The client waits for `/api/health`, completes local admin setup only when necessary, registers the health PostgreSQL database with a read-only role, creates collection `Health Agent`, dashboard `Анализы крови`, and one line card over `verified_lab_history` with date on X, normalized value on Y and canonical name as series. Existing objects are found by name and reused.

- [ ] **Step 3: Run unit and live smoke tests**

Run:

```bash
uv run pytest tests/test_metabase.py -q
uv run health-agent dashboard setup
curl --fail http://127.0.0.1:53000/api/health
```

Expected: unit test passes; Metabase reports healthy; repeated setup keeps one dashboard/card.

- [ ] **Step 4: Commit**

```bash
git add src/health_agent/metabase.py src/health_agent/cli.py tests/test_metabase.py
git commit -m "feat: provision first lab dashboard"
```

### Task 7: Prove the vertical slice on synthetic and real input

**Files:**
- Create: `tests/test_slice_1_e2e.py`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-09-04-health-agent-v1-slice-1-foundation.md`

**Interfaces:**
- Consumes: all Slice 1 CLI commands and views.
- Produces: repeatable evidence that one PDF reaches a verified dashboard row without duplicates.

- [ ] **Step 1: Add the automated synthetic journey**

The test creates a PDF in a temporary directory, imports it twice, approves one ferritin candidate and asserts: one vault object, one document, one observation, one audit decision and one row in `verified_lab_history`.

Run: `uv run pytest tests/test_slice_1_e2e.py -q`

Expected: PASS.

- [ ] **Step 2: Run all quality gates**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
```

Expected: all commands exit 0.

- [ ] **Step 3: Run one real document without committing it**

Copy one read-only source PDF into `data/incoming/`, run `health-agent import-file`, inspect the page evidence, approve only values that exactly match the source, and open `http://127.0.0.1:53000/dashboard/...`. If the document has no digital text, record `ocr_required` and use another real document for this slice; OCR becomes the first task of Slice 2 based on the observed layout.

Acceptance evidence contains only IDs, counts, statuses and a Metabase URL—never medical values or document text.

- [ ] **Step 4: Update the quick README and mark Slice 1 complete**

Document three commands only: start, import, open dashboard. Check completed boxes in this plan and add the acceptance date and safe counts.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_slice_1_e2e.py docs/superpowers/plans/2026-09-04-health-agent-v1-slice-1-foundation.md
git commit -m "test: prove first medical document vertical slice"
```
