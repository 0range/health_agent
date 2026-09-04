# Health Agent Views and Operations Implementation Plan

> **Status:** Reference detail only. Do not execute this document end-to-end; the authoritative scope is the lean v0.1 plan.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Довести локальный Personal Health Hub 0.1 до ежедневной эксплуатации: дать пользователю два восстанавливаемых Metabase-дашборда, автоматическую синхронизацию, безопасные статусы источников, шифрованные backup/restore и проверяемый runbook.

**Architecture:** План потребляет уже готовые медицинский импорт и wearable-коннекторы, добавляет над ними три стабильные read-only SQL-витрины и автоматически провиженит Metabase 0.63.16 из декларативных спецификаций. `launchd` запускает локальные сервисы, `health-hub sync all` и ежедневный backup; секреты остаются в локальных gitignored-файлах с правами `0600`, а логи содержат только разрешенные операционные поля.

**Tech Stack:** Python 3.12, uv, PostgreSQL 16, SQLAlchemy 2.0.52, Alembic 1.19.1, psycopg 3.2.10, httpx 0.28.1, cryptography 46.0.3, Metabase 0.63.16, Docker Compose, local mode-0600 secret files, launchd, pytest 9.1.1.

## Global Constraints

- Все проектные файлы создаются только внутри `health-agent/`; runtime-файлы находятся в gitignored `data/`.
- Этот план выполняется после `2026-09-04-health-agent-core-medical-import.md` и `2026-09-04-health-agent-wearable-connectors.md`.
- Входящая папка Google Drive остается read-only: ни один шаг не создает, не редактирует, не перемещает и не удаляет в ней файлы или папки.
- PostgreSQL на `127.0.0.1:55432` остается единственным источником правды; Metabase подключается отдельной ролью только к трем разрешенным SQL views.
- Используется ровно `metabase/metabase:v0.63.16`; порт Metabase публикуется только на `127.0.0.1:33000`.
- Сомнительные лабораторные результаты со статусом `needs_review` или `rejected` не попадают в дашборды.
- Референсные границы берутся из конкретного результата конкретной лаборатории; глобальная постоянная «норма» не вычисляется.
- У каждой точки анализа остаются `source_url`, `page_number` и `result_id`; отсутствие значения или референса остается `NULL`, а не превращается в норму или ноль.
- Полные wearable-ряды, API payloads, медицинский текст, имена файлов, OAuth tokens, пароли и ключи запрещено выводить в логи.
- Секреты Metabase/PostgreSQL и 256-битный ключ backup хранятся только в gitignored `data/secrets/`; каталогу назначается режим `0700`, каждому файлу — `0600`.
- Backup никогда не записывается во входящую Drive-папку и никогда не восстанавливается поверх production-базы `health_hub`.
- Все provisioning, sync, backup и restore операции идемпотентны либо явно отказываются от небезопасного повторения.
- Реальные медицинские документы и реальные API payloads не используются в автоматических тестах.

---

## Prerequisite interfaces consumed by this plan

План 1 предоставляет таблицы `source_connections`, `sync_runs`, `documents`, `lab_tests`, `lab_results`, `review_items` и CLI `health-hub publish sheets`.

План 2 предоставляет таблицы `wearable_daily`, `sleep_sessions`, `sleep_stages`, `workouts`, `workout_samples`, изолированную оркестрацию всех подключенных источников и CLI `health-hub sync all --mode backfill|incremental [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--dry-run]`. Команда `sync all` всегда продолжает остальные источники после ошибки одного из них.

SQL ниже использует этот зафиксированный межплановый контракт:

- `source_connections(id, provider, external_account_id, auth_status, granted_scopes, capabilities, sync_cursors, last_success_at, expected_interval_seconds)`;
- `sync_runs(id, connection_id, mode, status, requested_from, requested_to, pages_fetched, raw_created, normalized_created, normalized_updated, unchanged, failed, safe_error_code, safe_error_message, started_at, completed_at)`;
- `documents(id, source_url, laboratory_name)`;
- `lab_tests(id, canonical_code, display_name, category)`;
- `lab_results(id, lab_test_id, document_id, collected_at, normalized_value, normalized_unit, reference_low, reference_high, reference_text, page_number, status, superseded_result_id)`;
- `wearable_daily(id, connection_id, day, facet, source_timezone, recovery_score, readiness_score, activity_score, sleep_score, day_strain, steps, active_calories_kcal, total_calories_kcal, resting_hr_bpm, average_hr_bpm, hrv_rmssd_ms, respiratory_rate_rpm, spo2_percent, skin_temperature_delta_c, stress_high_seconds, vo2_max_ml_kg_min, source_values, raw_record_id, source_updated_at, status)` with uniqueness `(connection_id, day, facet)`;
- `sleep_sessions(id, connection_id, external_id, day, start_at, end_at, source_timezone, is_nap, score, performance_percent, efficiency_percent, time_in_bed_seconds, total_sleep_seconds, awake_seconds, latency_seconds, average_hr_bpm, lowest_hr_bpm, average_hrv_rmssd_ms, respiratory_rate_rpm, raw_record_id, source_updated_at, status)`;
- `workouts(id, connection_id, external_id, source_sport_code, source_sport_name, start_at, end_at, source_timezone, duration_seconds, distance_meters, energy_kcal, strain, training_load, average_hr_bpm, max_hr_bpm, elevation_gain_meters, source_values, raw_record_id, source_updated_at, status)`.

Если соседний план обнаружит конфликт имени до реализации, имена выравниваются в обоих планах до первого commit; совместимый alias внутри production-схемы не создается.

## File map

- `alembic/versions/0003_reporting_views.py` — выбор показателей и создание трех read-only views.
- `src/health_hub/reporting/sql/reporting_views.sql` — единственный SQL-контракт Metabase.
- `src/health_hub/reporting/status.py` — безопасная модель статусов и CLI serialization.
- `src/health_hub/operations/logging.py` — allowlist JSON-логирование без чувствительного содержимого.
- `src/health_hub/dashboard/client.py` — узкий клиент локального Metabase API.
- `src/health_hub/dashboard/provisioner.py` — идемпотентное создание collection, database, questions и dashboards.
- `src/health_hub/dashboard/spec.py` — декларативные спецификации двух дашбордов.
- `src/health_hub/dashboard/access.py` — отдельные PostgreSQL-роли Metabase app/read-only.
- `src/health_hub/security/local_secrets.py` — атомарные gitignored secrets с проверкой режимов `0700/0600`.
- `deploy/launchd/*.plist.template` — проверяемые шаблоны трех LaunchAgent jobs.
- `src/health_hub/operations/launchd.py` — рендеринг, установка и диагностика LaunchAgents.
- `scripts/run-services.sh`, `scripts/run-sync.sh`, `scripts/run-backup.sh` — минимальные launchd entrypoints.
- `src/health_hub/backup/crypto.py` — потоковый AES-256-GCM контейнер.
- `src/health_hub/backup/postgres.py` — строго аргументированные `pg_dump`/`pg_restore` процессы.
- `src/health_hub/backup/service.py` — атомарное создание, retention и безопасный restore drill.
- `scripts/acceptance-0.1.sh` — автоматизируемая сквозная приемка.
- `docs/runbooks/operations.md` — ежедневная эксплуатация, инциденты, backup и restore.

## Dependency and parallelization graph

Сначала завершается Task 1: SQL views являются общим контрактом. После него можно параллельно выполнять независимые ветви:

```text
Task 1 SQL views
├── Task 2 safe status/logging ──────────────────────────────┐
└── Task 3 Metabase runtime/local secrets ─┬── Task 4 overview dashboard ─┐
                                           ├── Task 5 blood dashboard ────┤
                                           └── Task 6 backup/restore ── Task 7 launchd
                                                                          │
                                             Task 2 ───────────────────────┤
                                                                          └── Task 8 acceptance/runbook
```

Tasks 2 и 3 безопасно поручать разным агентам сразу после merge Task 1. Tasks 4, 5 и 6 безопасно выполнять параллельно после merge Task 3. Tasks 4 и 5 независимо создают `overview.py`/`blood_tests.py` и свои тесты; назначенный интегратор Task 5 единолично вносит небольшие общие изменения в `spec.py`/`provisioner.py` после того, как оба новых файла доступны, и объединяет их в `all_dashboard_specs()`.

### Task 1: Create stable reporting views and dashboard indicator selection

**Files:**
- Create: `src/health_hub/reporting/__init__.py`
- Create: `src/health_hub/reporting/sql/reporting_views.sql`
- Create: `alembic/versions/0003_reporting_views.py`
- Test: `tests/reporting/test_reporting_views.py`

**Interfaces:**
- Consumes: prerequisite tables and columns listed above.
- Produces: table `dashboard_lab_selection`; views `dashboard_source_status`, `dashboard_daily_health`, `dashboard_lab_history`.
- Produces columns: `dashboard_lab_history(result_id, canonical_code, display_name, category, collected_on, value, unit, previous_value, delta, reference_low, reference_high, reference_text, reference_status, laboratory_name, source_url, page_number)`.

- [ ] **Step 1: Write failing PostgreSQL view contract tests**

```python
# tests/reporting/test_reporting_views.py
from decimal import Decimal

from sqlalchemy import text


def test_review_items_never_enter_lab_dashboard(pg_session, seeded_reporting_data):
    rows = pg_session.execute(text("SELECT result_id FROM dashboard_lab_history ORDER BY result_id")).scalars().all()
    assert rows == [seeded_reporting_data.verified_current_id]


def test_reference_status_is_source_specific(pg_session, seeded_reporting_data):
    row = pg_session.execute(text("""
        SELECT value, reference_low, reference_high, reference_status, source_url, page_number
        FROM dashboard_lab_history WHERE result_id = :id
    """), {"id": seeded_reporting_data.verified_current_id}).one()
    assert row == (Decimal("42"), Decimal("30"), Decimal("40"), "high", "https://drive.google.com/file/d/synthetic", 2)


def test_source_freshness_uses_each_connection_interval(pg_session, seeded_reporting_data):
    row = pg_session.execute(text("""
        SELECT health_status FROM dashboard_source_status WHERE source_key = 'oura'
    """)).scalar_one()
    assert row == "stale"
```

- [ ] **Step 2: Run the tests and confirm the views are absent**

Run: `uv run pytest tests/reporting/test_reporting_views.py -q`

Expected: FAIL with `relation "dashboard_lab_history" does not exist`.

- [ ] **Step 3: Create the indicator-selection table and seed a focused default set**

```python
# alembic/versions/0003_reporting_views.py
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0003_reporting_views"
down_revision = "0002_wearables"

DEFAULTS = [
    ("hemoglobin", "Общий анализ крови", 10),
    ("ferritin", "Обмен железа", 20),
    ("serum_iron", "Обмен железа", 30),
    ("transferrin_saturation", "Обмен железа", 40),
    ("total_cholesterol", "Липидный профиль", 50),
    ("ldl_cholesterol", "Липидный профиль", 60),
    ("hdl_cholesterol", "Липидный профиль", 70),
    ("triglycerides", "Липидный профиль", 80),
    ("apob", "Липидный профиль", 90),
    ("lipoprotein_a", "Липидный профиль", 100),
    ("glucose", "Углеводный обмен", 110),
    ("hba1c", "Углеводный обмен", 120),
    ("insulin", "Углеводный обмен", 130),
    ("vitamin_d_25_oh", "Витамины и минералы", 140),
    ("vitamin_b12", "Витамины и минералы", 150),
    ("folate", "Витамины и минералы", 160),
    ("tsh", "Щитовидная железа и гормоны", 170),
    ("free_t4", "Щитовидная железа и гормоны", 180),
    ("prolactin", "Щитовидная железа и гормоны", 190),
    ("alt", "Печень, почки и воспаление", 200),
    ("ast", "Печень, почки и воспаление", 210),
    ("creatinine", "Печень, почки и воспаление", 220),
    ("egfr", "Печень, почки и воспаление", 230),
    ("crp", "Печень, почки и воспаление", 240),
]


def upgrade() -> None:
    op.create_table(
        "dashboard_lab_selection",
        sa.Column("canonical_code", sa.Text(), primary_key=True),
        sa.Column("dashboard_group", sa.Text(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    table = sa.table("dashboard_lab_selection", sa.column("canonical_code"), sa.column("dashboard_group"), sa.column("display_order"), sa.column("enabled"))
    op.bulk_insert(table, [{"canonical_code": c, "dashboard_group": g, "display_order": o, "enabled": True} for c, g, o in DEFAULTS])
    op.execute(Path("src/health_hub/reporting/sql/reporting_views.sql").read_text())
```

Before running the migration, ensure the medical catalog contains every `canonical_code` above; `tests/labs/test_normalizer.py` from Plan 1 is the gate that enforces those rows.

- [ ] **Step 4: Define all three views with current-version and `NULL` semantics**

```sql
-- src/health_hub/reporting/sql/reporting_views.sql
CREATE OR REPLACE VIEW dashboard_source_status AS
WITH attempts AS (
    SELECT DISTINCT ON (connection_id)
        connection_id, id AS last_run_id, status AS last_run_status,
        started_at AS last_attempt_at, completed_at, normalized_created, normalized_updated,
        safe_error_code, safe_error_message
    FROM sync_runs
    ORDER BY connection_id, started_at DESC
), successes AS (
    SELECT connection_id, max(completed_at) AS last_success_at
    FROM sync_runs WHERE status = 'succeeded' GROUP BY connection_id
)
SELECT sc.provider AS source_key, upper(sc.provider) AS display_name, sc.auth_status,
       sc.expected_interval_seconds,
       a.last_run_id, a.last_run_status, a.last_attempt_at, s.last_success_at,
       a.normalized_created AS created_count, a.normalized_updated AS updated_count,
       a.safe_error_code AS error_code,
       CASE
         WHEN sc.auth_status <> 'connected' THEN 'disconnected'
         WHEN a.last_run_status = 'running' THEN 'running'
         WHEN s.last_success_at IS NULL THEN 'never_synced'
         WHEN a.last_run_status = 'failed' THEN 'failed'
         WHEN now() > s.last_success_at + make_interval(secs => sc.expected_interval_seconds) THEN 'stale'
         WHEN a.last_run_status = 'partial' THEN 'partial'
         ELSE 'healthy'
       END AS health_status,
       s.last_success_at + make_interval(secs => sc.expected_interval_seconds) AS stale_after_at
FROM source_connections sc
LEFT JOIN attempts a ON a.connection_id = sc.id
LEFT JOIN successes s ON s.connection_id = sc.id;

CREATE OR REPLACE VIEW dashboard_daily_health AS
WITH sleep_by_day AS (
    SELECT connection_id, day,
           sum(total_sleep_seconds) / 60.0 AS total_sleep_minutes,
           avg(efficiency_percent) AS sleep_efficiency_pct
    FROM sleep_sessions WHERE status = 'active' GROUP BY connection_id, day
), workouts_by_day AS (
    SELECT connection_id, start_at::date AS workout_date,
           count(*) AS workout_count,
           sum(duration_seconds) / 60.0 AS workout_minutes,
           sum(training_load) AS training_load
    FROM workouts WHERE status = 'active' GROUP BY connection_id, start_at::date
), daily AS (
    SELECT connection_id, day,
           max(resting_hr_bpm) AS resting_heart_rate_bpm,
           max(hrv_rmssd_ms) AS hrv_rmssd_ms, max(recovery_score) AS recovery_score,
           max(readiness_score) AS readiness_score, max(sleep_score) AS sleep_score,
           max(activity_score) AS activity_score, max(day_strain) AS strain_score,
           max(spo2_percent) AS spo2_average_pct
    FROM wearable_daily WHERE status = 'active' GROUP BY connection_id, day
)
SELECT wd.day AS metric_date, sc.provider AS source_key, wd.resting_heart_rate_bpm, wd.hrv_rmssd_ms,
       wd.recovery_score, wd.readiness_score, wd.sleep_score, wd.activity_score,
       wd.strain_score, wd.spo2_average_pct, sd.total_sleep_minutes,
       sd.sleep_efficiency_pct, coalesce(wbd.workout_count, 0) AS workout_count,
       coalesce(wbd.workout_minutes, 0) AS workout_minutes,
       wbd.training_load
FROM daily wd
JOIN source_connections sc ON sc.id = wd.connection_id
LEFT JOIN sleep_by_day sd ON sd.connection_id = wd.connection_id AND sd.day = wd.day
LEFT JOIN workouts_by_day wbd ON wbd.connection_id = wd.connection_id AND wbd.workout_date = wd.day;

CREATE OR REPLACE VIEW dashboard_lab_history AS
WITH current_results AS (
    SELECT lr.*
    FROM lab_results lr
    WHERE lr.status = 'verified'
      AND NOT EXISTS (SELECT 1 FROM lab_results newer WHERE newer.superseded_result_id = lr.id)
), with_previous AS (
    SELECT cr.*,
           lag(cr.normalized_value) OVER (PARTITION BY cr.lab_test_id, cr.normalized_unit ORDER BY cr.collected_at, cr.id) AS previous_value
    FROM current_results cr
)
SELECT wp.id AS result_id, lt.canonical_code, lt.display_name,
       dls.dashboard_group AS category, wp.collected_at::date AS collected_on,
       wp.normalized_value AS value, wp.normalized_unit AS unit,
       wp.previous_value, wp.normalized_value - wp.previous_value AS delta,
       wp.reference_low, wp.reference_high, wp.reference_text,
       CASE
         WHEN wp.reference_low IS NULL AND wp.reference_high IS NULL THEN 'unknown'
         WHEN wp.reference_low IS NOT NULL AND wp.normalized_value < wp.reference_low THEN 'low'
         WHEN wp.reference_high IS NOT NULL AND wp.normalized_value > wp.reference_high THEN 'high'
         ELSE 'within'
       END AS reference_status,
       d.laboratory_name, d.source_url, wp.page_number
FROM with_previous wp
JOIN lab_tests lt ON lt.id = wp.lab_test_id
JOIN dashboard_lab_selection dls ON dls.canonical_code = lt.canonical_code AND dls.enabled
JOIN documents d ON d.id = wp.document_id;
```

`downgrade()` drops the three views first and then `dashboard_lab_selection`.

- [ ] **Step 5: Apply the migration twice and verify query plans do not touch raw payload tables**

Run: `uv run alembic upgrade head && uv run alembic upgrade head && uv run pytest tests/reporting -q`

Expected: second migration is a no-op; all tests pass.

Run: `psql "$DATABASE_URL" -c "EXPLAIN SELECT * FROM dashboard_daily_health WHERE metric_date >= current_date - 30"`

Expected: plan references `wearable_daily`, `sleep_sessions`, `workouts`, `source_connections`; it does not reference `raw_records`, `sleep_stages` or `workout_samples`.

- [ ] **Step 6: Commit the reporting contract**

```bash
git add alembic/versions/0003_reporting_views.py src/health_hub/reporting tests/reporting
git commit -m "feat: add stable health dashboard views"
```

### Task 2: Add allowlist logging and a safe source-status command

**Files:**
- Create: `src/health_hub/operations/__init__.py`
- Create: `src/health_hub/operations/logging.py`
- Create: `src/health_hub/reporting/status.py`
- Modify: `src/health_hub/cli.py`
- Test: `tests/operations/test_safe_logging.py`
- Test: `tests/reporting/test_status.py`

**Interfaces:**
- Consumes: `dashboard_source_status`.
- Produces: `SafeEventLogger.event(name: str, *, source: str | None, run_id: str | None, status: str, counts: dict[str, int] | None, duration_ms: int | None, error_code: str | None) -> None`.
- Produces: `load_source_status(session) -> list[SourceStatus]`; CLI `health-hub status [--json]` with no sensitive fields.

- [ ] **Step 1: Write failing tests proving secrets and medical contents cannot be logged**

```python
# tests/operations/test_safe_logging.py
import json


def test_safe_logger_has_no_arbitrary_message_or_payload(tmp_path):
    logger = SafeEventLogger(tmp_path / "health-hub.jsonl")
    logger.event("sync_finished", source="oura", run_id="run-1", status="failed", counts={"created": 0}, duration_ms=41, error_code="oauth_expired")
    event = json.loads((tmp_path / "health-hub.jsonl").read_text())
    assert set(event) == {"timestamp", "event", "source", "run_id", "status", "counts", "duration_ms", "error_code"}


def test_unknown_exception_text_is_discarded():
    info = safe_error(RuntimeError("Bearer secret-token; Иванов; ferritin=7; report.pdf"))
    assert info.error_code == "unexpected_runtimeerror"
    assert info.safe_summary == "Unexpected RuntimeError; use run_id for diagnosis"
```

- [ ] **Step 2: Run tests and verify missing logger/status modules**

Run: `uv run pytest tests/operations/test_safe_logging.py tests/reporting/test_status.py -q`

Expected: FAIL with missing `SafeEventLogger` and `load_source_status`.

- [ ] **Step 3: Implement a closed logging schema and rotating local file**

```python
# src/health_hub/operations/logging.py
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from logging.handlers import RotatingFileHandler


@dataclass(frozen=True)
class SafeError:
    error_code: str
    safe_summary: str


KNOWN_ERRORS = {
    "TimeoutError": SafeError("source_timeout", "Source request timed out"),
    "PermissionError": SafeError("permission_denied", "Local operation was denied"),
}


def safe_error(exc: Exception) -> SafeError:
    name = type(exc).__name__
    return KNOWN_ERRORS.get(name, SafeError(f"unexpected_{name.lower()}", f"Unexpected {name}; use run_id for diagnosis"))


class SafeEventLogger:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handler = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def event(self, name, *, source=None, run_id=None, status, counts=None, duration_ms=None, error_code=None):
        record = {"timestamp": datetime.now(UTC).isoformat(), "event": name, "source": source, "run_id": run_id, "status": status, "counts": counts, "duration_ms": duration_ms, "error_code": error_code}
        log_record = logging.LogRecord("health_hub", logging.INFO, "", 0, json.dumps(record, ensure_ascii=False), (), None)
        self.handler.emit(log_record)
```

Do not expose a `message`, `payload`, `document`, `filename`, `url`, `token`, `exception` or `extra` argument. Connector exception handlers store only `safe_error(exc).error_code` and `.safe_summary` in `sync_runs`.

- [ ] **Step 4: Implement source status as typed rows and stable process exit codes**

```python
# src/health_hub/reporting/status.py
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import text


@dataclass(frozen=True)
class SourceStatus:
    source_key: str
    health_status: str
    last_success_at: datetime | None
    stale_after_at: datetime | None
    error_code: str | None


def load_source_status(session) -> list[SourceStatus]:
    rows = session.execute(text("""
        SELECT source_key, health_status, last_success_at, stale_after_at, error_code
        FROM dashboard_source_status ORDER BY source_key
    """)).mappings()
    return [SourceStatus(**row) for row in rows]


def status_exit_code(rows: list[SourceStatus]) -> int:
    return 2 if any(row.health_status in {"failed", "never_synced", "disconnected"} for row in rows) else 1 if any(row.health_status in {"stale", "partial"} for row in rows) else 0
```

Wire `health-hub status --json` to emit only `SourceStatus` fields. Human output uses one row per source and displays `—` for `NULL`; it never prints `safe_error_message`.

- [ ] **Step 5: Run privacy regression tests and CLI smoke test**

Run: `uv run pytest tests/operations tests/reporting -q && uv run health-hub status --json | uv run python -m json.tool`

Expected: tests pass; JSON is valid and contains no `token`, `payload`, `filename`, `source_url` or medical value keys.

- [ ] **Step 6: Commit observability**

```bash
git add src/health_hub/operations src/health_hub/reporting/status.py src/health_hub/cli.py tests/operations tests/reporting/test_status.py
git commit -m "feat: expose privacy-safe source status"
```

### Task 3: Pin Metabase 0.63.16 and build idempotent local provisioning

**Files:**
- Modify: `pyproject.toml`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `.gitignore`
- Create: `src/health_hub/security/local_secrets.py`
- Create: `src/health_hub/dashboard/__init__.py`
- Create: `src/health_hub/dashboard/client.py`
- Create: `src/health_hub/dashboard/access.py`
- Create: `src/health_hub/dashboard/provisioner.py`
- Modify: `src/health_hub/cli.py`
- Test: `tests/dashboard/test_client.py`
- Test: `tests/dashboard/test_access.py`
- Test: `tests/dashboard/test_provisioner.py`
- Test: `tests/security/test_local_secrets.py`

**Interfaces:**
- Consumes: three views from Task 1.
- Produces: `LocalSecretStore.get(name: str) -> str | None`, `require(name: str) -> str`, `set(name: str, value: str) -> None`, `get_or_create_bytes(name: str, length: int) -> bytes`.
- Produces: `MetabaseClient`, `ensure_reporting_role(admin_connection, password: str) -> None`, `provision_base(client, db_password: str) -> ProvisionedBase`.
- Produces: `ParameterSpec`, `Position`, `CardSpec`, `DashboardSpec`, `table_card()`, `line_card()`, `reference_band_card()`, `date_range_parameter()`, `date_parameter()`, `text_parameter()`.
- Produces: CLI `health-hub dashboard prepare` and `health-hub dashboard provision`.

- [ ] **Step 1: Write failing API idempotency and database-privilege tests**

```python
# tests/dashboard/test_provisioner.py
def test_base_provisioning_is_idempotent(fake_metabase, secret_store):
    first = provision_base(fake_metabase.client, "read-only-password")
    second = provision_base(fake_metabase.client, "read-only-password")
    assert first == second
    assert fake_metabase.count("collection", "Personal Health Hub") == 1
    assert fake_metabase.count("database", "Health Hub read-only") == 1


# tests/dashboard/test_access.py
def test_dashboard_role_can_select_views_but_not_base_tables(reporting_connection):
    assert reporting_connection.execute("SELECT count(*) FROM dashboard_lab_history").scalar() >= 0
    with pytest.raises(InsufficientPrivilege):
        reporting_connection.execute("SELECT count(*) FROM lab_results")


# tests/security/test_local_secrets.py
def test_local_secret_store_enforces_private_files(tmp_path):
    store = LocalSecretStore(tmp_path / "data" / "secrets")
    store.set("backup-encryption-key", "synthetic-value")
    path = tmp_path / "data" / "secrets" / "backup-encryption-key"
    assert stat.S_IMODE((tmp_path / "data" / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.get("backup-encryption-key") == "synthetic-value"
    assert "synthetic-value" not in repr(store)
```

- [ ] **Step 2: Run tests and verify provisioning is missing**

Run: `uv run pytest tests/dashboard/test_client.py tests/dashboard/test_access.py tests/dashboard/test_provisioner.py tests/security/test_local_secrets.py -q`

Expected: FAIL with missing dashboard modules.

- [ ] **Step 3: Pin client dependencies and local-only Metabase service**

Add `httpx==0.28.1` and `cryptography==46.0.3` to `[project].dependencies`, then run `uv lock`.

```yaml
# compose.yaml service addition
  metabase:
    image: metabase/metabase:v0.63.16
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "127.0.0.1:33000:3000"
    environment:
      MB_DB_TYPE: postgres
      MB_DB_HOST: postgres
      MB_DB_PORT: "5432"
      MB_DB_DBNAME: metabase_app
      MB_DB_USER: metabase_app
      MB_DB_PASS: ${METABASE_APP_DB_PASSWORD}
      MB_SITE_URL: http://127.0.0.1:33000
      MB_ANON_TRACKING_ENABLED: "false"
      MB_EMOJI_IN_LOGS: "false"
```

Add this healthcheck to the existing `postgres` service so `condition: service_healthy` is real:

```yaml
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U health_hub -d health_hub"]
      interval: 5s
      timeout: 3s
      retries: 20
```

Add only non-secret values to `.env.example`: `METABASE_URL=http://127.0.0.1:33000`, `METABASE_IMAGE=metabase/metabase:v0.63.16`, `METABASE_HEALTH_DB_HOST=postgres`, `METABASE_HEALTH_DB_PORT=5432`. Password fields remain absent. `data/` is already in `.gitignore`.

- [ ] **Step 4: Implement the minimal local secret store with strict permissions**

```python
# src/health_hub/security/local_secrets.py
class LocalSecretStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def get(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists():
            return None
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PermissionError(f"secret file must have mode 0600: {name}")
        return path.read_text(encoding="utf-8").rstrip("\n")

    def set(self, name: str, value: str) -> None:
        path = self._path(name)
        fd, temporary_name = tempfile.mkstemp(prefix=".secret-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value + "\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise FileNotFoundError(f"required local secret is missing: {name}")
        return value

    def get_or_create_bytes(self, name: str, length: int) -> bytes:
        stored = self.get(name)
        if stored is None:
            value = secrets.token_bytes(length)
            self.set(name, base64.urlsafe_b64encode(value).decode("ascii"))
            return value
        return self.require_bytes(name, length)

    def require_bytes(self, name: str, length: int) -> bytes:
        value = base64.urlsafe_b64decode(self.require(name).encode("ascii"))
        if len(value) != length:
            raise ValueError(f"local secret has invalid byte length: {name}")
        return value

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("invalid secret name")
        return self.root / name
```

`get_or_create_bytes` stores URL-safe base64 and checks the decoded length. Tests cover mode enforcement, path traversal rejection, atomic replacement and a `repr` with no secret values.

- [ ] **Step 5: Create least-privilege PostgreSQL roles and Metabase application database**

```python
# src/health_hub/dashboard/access.py
from psycopg import sql

REPORTING_VIEWS = ("dashboard_source_status", "dashboard_daily_health", "dashboard_lab_history")


def ensure_reporting_role(admin_connection, password: str) -> None:
    admin_connection.execute("DO $$ BEGIN CREATE ROLE health_dashboard LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    admin_connection.execute("ALTER ROLE health_dashboard PASSWORD %s", (password,))
    admin_connection.execute("REVOKE ALL ON SCHEMA public FROM health_dashboard")
    admin_connection.execute("GRANT USAGE ON SCHEMA public TO health_dashboard")
    admin_connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM health_dashboard")
    for view in REPORTING_VIEWS:
        admin_connection.execute(sql.SQL("GRANT SELECT ON {} TO health_dashboard").format(sql.Identifier(view)))
```

`health-hub dashboard prepare` creates database `metabase_app` and login `metabase_app` using identifiers fixed in code, generates both 32-byte passwords once, and stores them as `data/secrets/metabase-app-db-password` and `data/secrets/metabase-health-db-password`. Passwords are bound parameters or process environment values, never command-line arguments.

- [ ] **Step 6: Implement a narrow Metabase API client and first-run setup**

```python
# src/health_hub/dashboard/client.py
class MetabaseClient:
    def __init__(self, base_url: str, http, session_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.session_id = session_id

    def request(self, method: str, path: str, json: dict | None = None):
        headers = {"X-Metabase-Session": self.session_id} if self.session_id else {}
        response = self.http.request(method, f"{self.base_url}{path}", headers=headers, json=json, timeout=30)
        response.raise_for_status()
        return response.json() if response.content else None

    def find_one(self, path: str, *, name: str):
        return next((item for item in self.request("GET", path) if item.get("name") == name), None)
```

On an uninitialized instance, read `/api/session/properties`, use its `setup-token` once at `/api/setup`, and save generated admin email/password as `data/secrets/metabase-admin-email` and `data/secrets/metabase-admin-password`. On initialized runs, create a session through `/api/session`. `repr(MetabaseClient)` must omit the session id.

- [ ] **Step 7: Implement base provisioning by lookup-and-update, never blind create**

Define the complete cross-dashboard types before the provisioner:

```python
# src/health_hub/dashboard/spec.py
@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: str
    default: str | None = None


@dataclass(frozen=True)
class Position:
    row: int
    col: int
    width: int
    height: int


@dataclass(frozen=True)
class CardSpec:
    name: str
    sql: str
    kind: str
    position: Position
    visualization_settings: dict
    template_tags: dict


@dataclass(frozen=True)
class DashboardSpec:
    name: str
    collection_id: int
    database_id: int
    parameters: list[ParameterSpec]
    cards: list[CardSpec]


TAG_TYPES = {
    "period": ("date", "date/all-options"),
    "start_date": ("date", "date/single"),
    "end_date": ("date", "date/single"),
    "laboratory": ("text", "string/="),
}


def field_filter_tags(sql: str) -> dict:
    tags = {}
    for name, (tag_type, widget_type) in TAG_TYPES.items():
        if "{{" + name + "}}" in sql:
            tags[name] = {"id": str(uuid5(NAMESPACE_URL, f"personal-health-hub:{name}")), "name": name, "display-name": name.replace("_", " ").title(), "type": tag_type, "widget-type": widget_type, "required": False}
    unresolved = set(re.findall(r"\{\{([a-z_]+)\}\}", sql)) - set(tags)
    if unresolved:
        raise ValueError(f"unknown Metabase template tags: {sorted(unresolved)}")
    return tags


def table_card(name, sql, *, row, col, width, height):
    return CardSpec(name, sql, "table", Position(row, col, width, height), {}, {})


def line_card(name, sql, *, row, col, width, height):
    return CardSpec(name, sql, "line", Position(row, col, width, height), {}, field_filter_tags(sql))


def reference_band_card(*, name, sql, row, col, width, height, series, click_url_field):
    settings = {"graph.series_settings": series, "click_behavior": {"type": "link", "linkType": "url", "parameterMapping": {"url": click_url_field}}}
    return CardSpec(name, sql, "line", Position(row, col, width, height), settings, field_filter_tags(sql))


def date_range_parameter(name, default=None):
    return ParameterSpec(name, "date/all-options", default)


def date_parameter(name):
    return ParameterSpec(name, "date/single", None)


def text_parameter(name):
    return ParameterSpec(name, "string/=", None)
```

`field_filter_tags(sql)` above returns stable Metabase native-query template tags and rejects unknown tags during provisioning.

```python
# src/health_hub/dashboard/provisioner.py
@dataclass(frozen=True)
class ProvisionedBase:
    collection_id: int
    database_id: int


def provision_base(client: MetabaseClient, db_password: str) -> ProvisionedBase:
    collection = ensure_named(client, "/api/collection", "Personal Health Hub", {"name": "Personal Health Hub", "color": "#509EE3"})
    database = ensure_named(client, "/api/database", "Health Hub read-only", {
        "name": "Health Hub read-only",
        "engine": "postgres",
        "details": {"host": "postgres", "port": 5432, "dbname": "health_hub", "user": "health_dashboard", "password": db_password, "ssl": False},
        "is_full_sync": False,
        "is_on_demand": False,
    })
    client.request("POST", f"/api/database/{database['id']}/sync_schema")
    return ProvisionedBase(collection_id=collection["id"], database_id=database["id"])
```

`ensure_named` updates an existing app-owned entity when its declarative fingerprint changed and creates it only when absent. Store stable entity IDs and SHA-256 fingerprints in `data/metabase/provisioning-state.json` with mode `0600`; this state contains no passwords or query results.

- [ ] **Step 8: Verify version pin, loopback binding, idempotency and grants**

Run: `METABASE_APP_DB_PASSWORD=synthetic-config-check docker compose config | grep -F 'metabase/metabase:v0.63.16' && METABASE_APP_DB_PASSWORD=synthetic-config-check docker compose config | grep -F '127.0.0.1:33000'`

Expected: both exact values are present.

Run: `uv run pytest tests/dashboard/test_client.py tests/dashboard/test_access.py tests/dashboard/test_provisioner.py tests/security/test_local_secrets.py -q`

Expected: all tests pass.

- [ ] **Step 9: Commit the Metabase foundation**

```bash
git add pyproject.toml uv.lock compose.yaml .env.example .gitignore src/health_hub/security/local_secrets.py src/health_hub/dashboard src/health_hub/cli.py tests/dashboard tests/security/test_local_secrets.py
git commit -m "feat: provision local read-only metabase"
```

### Task 4: Provision the Health Overview dashboard

**Files:**
- Create: `src/health_hub/dashboard/overview.py`
- Modify: `src/health_hub/dashboard/spec.py`
- Modify: `src/health_hub/dashboard/provisioner.py`
- Test: `tests/dashboard/test_overview.py`

**Interfaces:**
- Consumes: `dashboard_source_status`, `dashboard_daily_health`, `dashboard_lab_history`, `ProvisionedBase`.
- Produces: `overview_spec(database_id: int, collection_id: int) -> DashboardSpec` named `Обзор здоровья`.

- [ ] **Step 1: Write the failing dashboard contract test**

```python
# tests/dashboard/test_overview.py
def test_overview_contains_required_cards():
    spec = overview_spec(database_id=7, collection_id=11)
    assert spec.name == "Обзор здоровья"
    assert [card.name for card in spec.cards] == [
        "Источники — состояние",
        "Последние ключевые анализы",
        "Сон — продолжительность и эффективность",
        "HRV и пульс в покое",
        "Recovery, readiness и sleep score",
        "Тренировочная нагрузка",
    ]
    assert all("raw_records" not in card.sql for card in spec.cards)
```

- [ ] **Step 2: Run the test and verify the spec is missing**

Run: `uv run pytest tests/dashboard/test_overview.py -q`

Expected: FAIL with missing `overview_spec`.

- [ ] **Step 3: Define explicit cards, grid positions and 90-day filters**

```python
# src/health_hub/dashboard/overview.py
def overview_spec(database_id: int, collection_id: int) -> DashboardSpec:
    return DashboardSpec(
        name="Обзор здоровья",
        collection_id=collection_id,
        parameters=[date_parameter("start_date"), date_parameter("end_date")],
        cards=[
            table_card("Источники — состояние", "SELECT display_name, health_status, last_success_at, stale_after_at, error_code FROM dashboard_source_status ORDER BY display_name", row=0, col=0, width=24, height=5),
            table_card("Последние ключевые анализы", "SELECT DISTINCT ON (canonical_code) display_name, value, unit, reference_status, collected_on, delta, source_url, page_number FROM dashboard_lab_history ORDER BY canonical_code, collected_on DESC", row=5, col=0, width=24, height=6),
            line_card("Сон — продолжительность и эффективность", "SELECT metric_date, source_key, total_sleep_minutes / 60.0 AS sleep_hours, sleep_efficiency_pct FROM dashboard_daily_health WHERE metric_date >= current_date - interval '90 days' [[AND metric_date >= {{start_date}}]] [[AND metric_date <= {{end_date}}]] ORDER BY metric_date", row=11, col=0, width=12, height=7),
            line_card("HRV и пульс в покое", "SELECT metric_date, source_key, hrv_rmssd_ms, resting_heart_rate_bpm FROM dashboard_daily_health WHERE metric_date >= current_date - interval '90 days' [[AND metric_date >= {{start_date}}]] [[AND metric_date <= {{end_date}}]] ORDER BY metric_date", row=11, col=12, width=12, height=7),
            line_card("Recovery, readiness и sleep score", "SELECT metric_date, source_key, recovery_score, readiness_score, sleep_score FROM dashboard_daily_health WHERE metric_date >= current_date - interval '90 days' [[AND metric_date >= {{start_date}}]] [[AND metric_date <= {{end_date}}]] ORDER BY metric_date", row=18, col=0, width=12, height=7),
            line_card("Тренировочная нагрузка", "SELECT metric_date, source_key, workout_count, workout_minutes, training_load, strain_score FROM dashboard_daily_health WHERE metric_date >= current_date - interval '90 days' [[AND metric_date >= {{start_date}}]] [[AND metric_date <= {{end_date}}]] ORDER BY metric_date", row=18, col=12, width=12, height=7),
        ],
        database_id=database_id,
    )
```

The `start_date` and `end_date` parameters bind to every wearable card; without user input, SQL displays the latest 90 days. `NULL` series remain gaps. The source-status table applies colors: `healthy` green, `running` blue, `stale`/`partial` amber, `failed`/`never_synced` red, `disconnected` gray.

- [ ] **Step 4: Add idempotent question/dashboard upserts**

```python
def provision_dashboard(client, spec: DashboardSpec) -> int:
    dashboard = ensure_dashboard(client, spec)
    existing = {card["card"]["name"]: card for card in client.request("GET", f"/api/dashboard/{dashboard['id']}")["dashcards"]}
    for card_spec in spec.cards:
        question = ensure_question(client, card_spec, spec.database_id, spec.collection_id)
        ensure_dashcard(client, dashboard["id"], question["id"], card_spec.position, existing.get(card_spec.name))
    delete_app_owned_dashcards_not_in_spec(client, dashboard["id"], set(card.name for card in spec.cards))
    return dashboard["id"]
```

Only entities tagged in `description` with `managed-by=personal-health-hub` can be updated or deleted; user-created Metabase cards are untouched.

- [ ] **Step 5: Run unit tests and provision twice against local Metabase**

Run: `uv run pytest tests/dashboard/test_overview.py tests/dashboard/test_provisioner.py -q`

Expected: tests pass.

Run: `uv run health-hub dashboard provision && uv run health-hub dashboard provision`

Expected: both runs exit 0; the second reports `created=0`, and `http://127.0.0.1:33000` contains exactly one dashboard named `Обзор здоровья`.

- [ ] **Step 6: Commit the overview dashboard**

```bash
git add src/health_hub/dashboard/overview.py src/health_hub/dashboard/spec.py src/health_hub/dashboard/provisioner.py tests/dashboard/test_overview.py
git commit -m "feat: provision health overview dashboard"
```

### Task 5: Provision multi-year blood-test charts with per-result reference bands

**Files:**
- Create: `src/health_hub/dashboard/blood_tests.py`
- Modify: `src/health_hub/dashboard/spec.py`
- Modify: `src/health_hub/dashboard/provisioner.py`
- Test: `tests/dashboard/test_blood_tests.py`

**Interfaces:**
- Consumes: enabled rows from `dashboard_lab_selection` and `dashboard_lab_history`.
- Produces: `blood_tests_spec(database_id: int, collection_id: int, indicators: list[Indicator]) -> DashboardSpec` named `Анализы крови — динамика по годам`.

- [ ] **Step 1: Write failing tests for one chart per selected indicator and provenance**

```python
# tests/dashboard/test_blood_tests.py
def test_each_enabled_indicator_gets_a_multi_year_chart():
    indicators = [Indicator("ferritin", "Ферритин", "Обмен железа", 20), Indicator("prolactin", "Пролактин", "Гормоны", 30)]
    spec = blood_tests_spec(7, 11, indicators)
    assert [card.name for card in spec.cards if card.kind == "line"] == ["Ферритин — по годам", "Пролактин — по годам"]
    for card in spec.cards:
        assert "status = 'verified'" not in card.sql
        assert "dashboard_lab_history" in card.sql


def test_chart_query_keeps_source_specific_bounds_and_link():
    sql = indicator_chart_sql("ferritin")
    assert all(column in sql for column in ("reference_low", "reference_high", "outside_value", "source_url", "page_number", "result_id"))
    assert "canonical_code = 'ferritin'" in sql
```

- [ ] **Step 2: Run the tests and verify blood dashboard code is absent**

Run: `uv run pytest tests/dashboard/test_blood_tests.py -q`

Expected: FAIL with missing `blood_tests_spec`.

- [ ] **Step 3: Build parameterized SQL without inventing missing reference ranges**

```python
# src/health_hub/dashboard/blood_tests.py
ALLOWED_CODE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Indicator:
    canonical_code: str
    display_name: str
    dashboard_group: str
    display_order: int


def indicator_chart_sql(code: str) -> str:
    if not ALLOWED_CODE.fullmatch(code):
        raise ValueError("invalid canonical indicator code")
    return f"""
        SELECT collected_on, value, reference_low, reference_high,
               CASE WHEN reference_status IN ('low', 'high') THEN value END AS outside_value,
               laboratory_name, source_url, page_number, result_id
        FROM dashboard_lab_history
        WHERE canonical_code = '{code}'
          [[AND collected_on >= {{{{start_date}}}}]]
          [[AND collected_on <= {{{{end_date}}}}]]
          [[AND laboratory_name = {{{{laboratory}}}}]]
        ORDER BY collected_on, result_id
    """.strip()
```

The chart plots `value` as the main line, `reference_low` and `reference_high` as dashed boundaries, and `outside_value` as red points. When one or both bounds are `NULL`, that part of the band stays absent. Tooltip includes laboratory, original unit, page and `result_id`; dynamic click behavior opens `source_url`.

- [ ] **Step 4: Build the dashboard from the database selection in stable group/order**

```python
def blood_tests_spec(database_id: int, collection_id: int, indicators: list[Indicator]) -> DashboardSpec:
    cards = []
    for index, indicator in enumerate(sorted(indicators, key=lambda item: item.display_order)):
        cards.append(reference_band_card(
            name=f"{indicator.display_name} — по годам",
            sql=indicator_chart_sql(indicator.canonical_code),
            row=(index // 2) * 8,
            col=(index % 2) * 12,
            width=12,
            height=8,
            series={
                "value": {"color": "#509EE3", "line.marker_enabled": True},
                "reference_low": {"color": "#88BF4D", "line.style": "dashed"},
                "reference_high": {"color": "#88BF4D", "line.style": "dashed"},
                "outside_value": {"color": "#ED6E6E", "line.marker_enabled": True, "line.missing": "none"},
            },
            click_url_field="source_url",
        ))
    return DashboardSpec(
        name="Анализы крови — динамика по годам",
        collection_id=collection_id,
        database_id=database_id,
        parameters=[date_parameter("start_date"), date_parameter("end_date"), text_parameter("laboratory")],
        cards=cards,
    )
```

Fetch indicators with `SELECT dls.canonical_code, lt.display_name, dls.dashboard_group, dls.display_order FROM dashboard_lab_selection dls JOIN lab_tests lt USING (canonical_code) WHERE dls.enabled ORDER BY dls.display_order`. Adding or disabling an indicator changes cards on the next `health-hub dashboard provision` without a schema migration.

After both parallel dashboard files are merged, the Task 5 integrator adds the single production catalog and wires the CLI to iterate it:

```python
# src/health_hub/dashboard/provisioner.py
def all_dashboard_specs(database_id: int, collection_id: int, indicators: list[Indicator]) -> list[DashboardSpec]:
    return [
        overview_spec(database_id, collection_id),
        blood_tests_spec(database_id, collection_id, indicators),
    ]


def provision_all_dashboards(client, base: ProvisionedBase, indicators: list[Indicator]) -> list[int]:
    specs = all_dashboard_specs(base.database_id, base.collection_id, indicators)
    return [provision_dashboard(client, spec) for spec in specs]
```

`health-hub dashboard provision` calls `provision_base`, loads enabled indicators and then `provision_all_dashboards`; the returned list must contain exactly two IDs.

- [ ] **Step 5: Verify reference provenance, missing data and dashboard idempotency**

Run: `uv run pytest tests/dashboard/test_blood_tests.py tests/reporting/test_reporting_views.py -q`

Expected: tests pass; `needs_review` rows and superseded rows are absent; missing ranges stay `NULL`.

Run: `uv run health-hub dashboard provision && uv run health-hub dashboard provision`

Expected: second run reports `created=0`; the dashboard has exactly one chart per enabled indicator, with date/laboratory filters and source-link click behavior.

- [ ] **Step 6: Commit the blood-test dashboard**

```bash
git add src/health_hub/dashboard/blood_tests.py src/health_hub/dashboard/spec.py src/health_hub/dashboard/provisioner.py tests/dashboard/test_blood_tests.py
git commit -m "feat: chart multi-year blood test history"
```

### Task 6: Create encrypted local backups and prove safe restore

**Files:**
- Modify: `pyproject.toml`
- Create: `src/health_hub/backup/__init__.py`
- Create: `src/health_hub/backup/crypto.py`
- Create: `src/health_hub/backup/postgres.py`
- Create: `src/health_hub/backup/service.py`
- Modify: `src/health_hub/cli.py`
- Test: `tests/backup/test_crypto.py`
- Test: `tests/backup/test_postgres.py`
- Test: `tests/backup/test_service.py`
- Test: `tests/backup/test_restore_integration.py`

**Interfaces:**
- Consumes: `LocalSecretStore` from Task 3.
- Produces: `encrypt_file(source: Path, target: Path, key: bytes) -> EncryptionResult`; `decrypt_file(source: Path, target: Path, key: bytes) -> None`.
- Produces: `BackupService.create() -> BackupArtifact`; `BackupService.restore(archive: Path, target_database: str, replace: bool = False) -> RestoreReport`.
- Produces: CLI `health-hub backup create` and `health-hub backup restore ARCHIVE --target-database NAME [--replace]`.

- [ ] **Step 1: Write failing encryption, process safety and production-refusal tests**

```python
# tests/backup/test_crypto.py
def test_aes_gcm_round_trip_and_tamper_detection(tmp_path):
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"synthetic-postgres-dump")
    encrypted = tmp_path / "backup.dump.enc"
    restored = tmp_path / "restored.dump"
    encrypt_file(source, encrypted, key)
    decrypt_file(encrypted, restored, key)
    assert restored.read_bytes() == source.read_bytes()
    encrypted.write_bytes(encrypted.read_bytes()[:-1] + b"x")
    with pytest.raises(InvalidTag):
        decrypt_file(encrypted, restored, key)


# tests/backup/test_service.py
def test_restore_refuses_production_name(service, archive):
    with pytest.raises(UnsafeRestoreTarget, match="health_hub_restore_"):
        service.restore(archive, target_database="health_hub", replace=True)
```

- [ ] **Step 2: Run tests and verify backup modules are absent**

Run: `uv run pytest tests/backup -q`

Expected: FAIL with missing backup modules.

- [ ] **Step 3: Implement an authenticated streaming container**

```python
# src/health_hub/backup/crypto.py
MAGIC = b"PHHBKUP1"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024


def encrypt_file(source: Path, target: Path, key: bytes) -> EncryptionResult:
    if len(key) != 32:
        raise ValueError("backup key must contain 32 bytes")
    nonce = os.urandom(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with source.open("rb") as src, target.open("xb") as dst:
        os.chmod(target, 0o600)
        dst.write(MAGIC + nonce)
        for chunk in iter(lambda: src.read(CHUNK_BYTES), b""):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)
    return EncryptionResult(path=target, sha256=sha256_file(target), bytes_written=target.stat().st_size)
```

`decrypt_file` validates `MAGIC`, reads the final 16-byte GCM tag, writes to an exclusive mode-0600 temporary file and atomically renames only after authentication succeeds. An invalid tag deletes only that temporary plaintext.

- [ ] **Step 4: Isolate PostgreSQL subprocesses from shell expansion and logs**

```python
# src/health_hub/backup/postgres.py
class PostgresRunner:
    def dump(self, database: str, target: Path, password: str) -> None:
        env = {**self.base_env, "PGPASSWORD": password}
        subprocess.run(["pg_dump", "--host", "127.0.0.1", "--port", "55432", "--username", "health_hub", "--format", "custom", "--file", str(target), database], env=env, check=True, capture_output=True, text=True)

    def restore(self, database: str, source: Path, password: str) -> None:
        env = {**self.base_env, "PGPASSWORD": password}
        subprocess.run(["pg_restore", "--host", "127.0.0.1", "--port", "55432", "--username", "health_hub", "--dbname", database, "--clean", "--if-exists", "--no-owner", str(source)], env=env, check=True, capture_output=True, text=True)
```

On failure, discard `stdout`/`stderr` and log only `backup_dump_failed` or `backup_restore_failed`. Tests inspect the exact argv and prove the password appears only in the child environment.

- [ ] **Step 5: Implement atomic create, retention and guarded restore**

```python
# src/health_hub/backup/service.py
SAFE_TARGET = re.compile(r"^health_hub_restore_[a-z0-9_]{1,40}$")


def create(self) -> BackupArtifact:
    key = self.secrets.get_or_create_bytes("backup-encryption-key", length=32)
    timestamp = self.clock.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = self.backup_dir / f"health-hub-{timestamp}.dump.enc"
    with TemporaryDirectory(dir=self.backup_dir) as tmp:
        dump = Path(tmp) / "health_hub.dump"
        self.postgres.dump("health_hub", dump, self.secrets.require("postgres-password"))
        result = encrypt_file(dump, final.with_suffix(".enc.partial"), key)
        final.with_suffix(".enc.partial").replace(final)
    self.prune_successful_backups(keep=30, exclude={final})
    return BackupArtifact(path=final, sha256=sha256_file(final), size=final.stat().st_size)


def restore(self, archive: Path, target_database: str, replace: bool = False) -> RestoreReport:
    if not SAFE_TARGET.fullmatch(target_database):
        raise UnsafeRestoreTarget("target must match health_hub_restore_[a-z0-9_]+")
    self.postgres.create_empty_database(target_database, replace=replace)
    with TemporaryDirectory(dir=self.backup_dir) as tmp:
        dump = Path(tmp) / "restore.dump"
        decrypt_file(archive, dump, self.secrets.require_bytes("backup-encryption-key", length=32))
        self.postgres.restore(target_database, dump, self.secrets.require("postgres-password"))
    return self.verify_restore(target_database)
```

Retention deletes only files matching `health-hub-????????T??????Z.dump.enc` inside the resolved configured backup directory after a new backup passed encryption verification. It keeps the newest 30. The restore verification requires Alembic revision `0003_reporting_views`, all three dashboard views, and nonnegative counts for `documents`, `lab_results`, `wearable_daily`, `sleep_sessions`, `workouts`.

`self.secrets` is `LocalSecretStore(PROJECT_ROOT / "data" / "secrets")` from Task 3. The first `backup create` generates `data/secrets/backup-encryption-key` once; subsequent runs reuse it. The backup is unrecoverable without this local key, so the runbook requires one explicit offline copy of that single key file outside the incoming Drive archive.

- [ ] **Step 6: Run unit tests and a real disposable restore drill**

Run: `uv run pytest tests/backup -q`

Expected: all tests pass, including tamper detection and refusal to target `health_hub`.

Run: `BACKUP_PATH=$(uv run health-hub backup create --print-path) && uv run health-hub backup restore "$BACKUP_PATH" --target-database health_hub_restore_acceptance --replace`

Expected: exit 0; report says `schema_revision=0003_reporting_views`, `views=3`, and prints counts only, not rows.

- [ ] **Step 7: Commit backup and restore**

```bash
git add pyproject.toml uv.lock src/health_hub/backup src/health_hub/cli.py tests/backup
git commit -m "feat: add encrypted postgres backup and restore drill"
```

### Task 7: Schedule services, isolated syncs and daily backups with launchd

**Files:**
- Create: `deploy/launchd/com.personal-health-hub.services.plist.template`
- Create: `deploy/launchd/com.personal-health-hub.sync.plist.template`
- Create: `deploy/launchd/com.personal-health-hub.backup.plist.template`
- Create: `src/health_hub/operations/launchd.py`
- Create: `scripts/run-services.sh`
- Create: `scripts/run-sync.sh`
- Create: `scripts/run-backup.sh`
- Modify: `src/health_hub/cli.py`
- Test: `tests/operations/test_launchd.py`
- Test: `tests/operations/test_entrypoints.py`

**Interfaces:**
- Consumes: `health-hub sync medical --apply`, `health-hub sync all --mode incremental`, `health-hub publish sheets`, `health-hub backup create`, `data/secrets/` and Docker Compose.
- Produces: `render_agents(project_root: Path, python_path: Path) -> dict[str, bytes]`; CLI `health-hub operations install|uninstall|status`.

- [ ] **Step 1: Write failing plist schedule and entrypoint tests**

```python
# tests/operations/test_launchd.py
def test_rendered_agents_have_exact_schedules_and_no_secrets(tmp_path):
    agents = render_agents(PROJECT_ROOT, Path("/opt/homebrew/bin/uv"))
    sync = plistlib.loads(agents["com.personal-health-hub.sync"])
    backup = plistlib.loads(agents["com.personal-health-hub.backup"])
    assert sync["StartInterval"] == 14400
    assert sync["RunAtLoad"] is True
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    serialized = repr(agents).lower()
    assert all(word not in serialized for word in ("password", "token", "secret", "authorization"))
```

- [ ] **Step 2: Run tests and verify launchd support is absent**

Run: `uv run pytest tests/operations/test_launchd.py tests/operations/test_entrypoints.py -q`

Expected: FAIL with missing `render_agents`.

- [ ] **Step 3: Add minimal scripts with strict permissions and safe exit behavior**

```bash
# scripts/run-sync.sh
#!/bin/zsh
set -euo pipefail
umask 077
cd "${0:A:h}/.."
sync_status=0
publish_status=0
/opt/homebrew/bin/uv run health-hub sync medical --apply || sync_status=$?
/opt/homebrew/bin/uv run health-hub sync all --mode incremental || sync_status=$?
/opt/homebrew/bin/uv run health-hub publish sheets || publish_status=$?
/opt/homebrew/bin/uv run health-hub status --json >/dev/null || true
if (( sync_status != 0 )); then
  exit "$sync_status"
fi
exit "$publish_status"
```

```bash
# scripts/run-backup.sh
#!/bin/zsh
set -euo pipefail
umask 077
cd "${0:A:h}/.."
/opt/homebrew/bin/uv run health-hub backup create
```

```bash
# scripts/run-services.sh
#!/bin/zsh
set -euo pipefail
umask 077
cd "${0:A:h}/.."
export METABASE_APP_DB_PASSWORD="$(<data/secrets/metabase-app-db-password)"
/usr/local/bin/docker compose up -d postgres metabase
unset METABASE_APP_DB_PASSWORD
```

The installer verifies `data/secrets` mode `0700` and each required secret mode `0600`, resolves actual `uv` and `docker` locations with `shutil.which`, writes those absolute paths into rendered scripts under `data/runtime/bin/`, mode `0700`, and never persists the expanded secret. Repository scripts remain templates and are not invoked directly by launchd.

- [ ] **Step 4: Render three LaunchAgent property lists**

```xml
<!-- deploy/launchd/com.personal-health-hub.sync.plist.template -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.personal-health-hub.sync</string>
  <key>ProgramArguments</key><array><string>__RUNTIME_BIN__/run-sync.sh</string></array>
  <key>WorkingDirectory</key><string>__PROJECT_ROOT__</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>14400</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
```

Services uses `RunAtLoad=true`; backup uses `StartCalendarInterval={Hour=3, Minute=30}`. Each job has a distinct label, no `KeepAlive` restart loop, no network listener arguments and no secrets. Application events continue to the rotating JSONL file from Task 2.

- [ ] **Step 5: Implement install/uninstall/status without touching unrelated agents**

```python
# src/health_hub/operations/launchd.py
LABELS = (
    "com.personal-health-hub.services",
    "com.personal-health-hub.sync",
    "com.personal-health-hub.backup",
)


def install_agents(rendered: dict[str, bytes], launch_agents: Path, runner: Runner, uid: int) -> None:
    launch_agents.mkdir(parents=True, exist_ok=True)
    for label in LABELS:
        target = launch_agents / f"{label}.plist"
        atomic_write(target, rendered[label], mode=0o600)
        runner.run(["launchctl", "bootout", f"gui/{uid}/{label}"], allow_not_found=True)
        runner.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)])


def uninstall_agents(launch_agents: Path, runner: Runner, uid: int) -> None:
    for label in LABELS:
        runner.run(["launchctl", "bootout", f"gui/{uid}/{label}"], allow_not_found=True)
        (launch_agents / f"{label}.plist").unlink(missing_ok=True)
```

`operations status` calls `launchctl print gui/$UID/<label>` and reports `loaded|not_loaded` plus last process exit status. It does not print the job environment.

- [ ] **Step 6: Run tests, install agents and force one safe run of each**

Run: `uv run pytest tests/operations -q && uv run health-hub operations install`

Expected: tests pass and exactly three project agents are installed.

Run: `launchctl kickstart -k "gui/$UID/com.personal-health-hub.services" && launchctl kickstart -k "gui/$UID/com.personal-health-hub.sync" && launchctl kickstart -k "gui/$UID/com.personal-health-hub.backup"`

Expected: services become reachable locally; a sync run and encrypted backup appear; Drive remains read-only because its OAuth scope is unchanged.

- [ ] **Step 7: Commit scheduling**

```bash
git add deploy/launchd src/health_hub/operations/launchd.py scripts/run-services.sh scripts/run-sync.sh scripts/run-backup.sh src/health_hub/cli.py tests/operations
git commit -m "feat: schedule local sync and backups with launchd"
```

### Task 8: Automate acceptance and write the complete operations runbook

**Files:**
- Create: `scripts/acceptance-0.1.sh`
- Create: `tests/acceptance/test_job_zero.py`
- Create: `docs/runbooks/operations.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all Plan 1, Plan 2 and Tasks 1–7 CLI commands and views.
- Produces: repeatable acceptance command `scripts/acceptance-0.1.sh`; operator source of truth `docs/runbooks/operations.md`.

- [ ] **Step 1: Write the failing machine-verifiable acceptance test**

```python
# tests/acceptance/test_job_zero.py
def test_job_zero_contract(pg_session, metabase_client, launchd_probe, latest_backup):
    assert pg_session.scalar(text("SELECT count(*) FROM documents")) >= 0
    assert pg_session.scalar(text("SELECT count(*) FROM dashboard_lab_history WHERE source_url IS NULL OR page_number IS NULL")) == 0
    assert pg_session.scalar(text("SELECT count(*) FROM dashboard_lab_history h JOIN lab_results r ON r.id=h.result_id WHERE r.status <> 'verified'")) == 0
    assert {row.source_key for row in load_source_status(pg_session)} >= {"google_drive", "whoop", "oura", "coros"}
    assert metabase_client.dashboard_names() >= {"Обзор здоровья", "Анализы крови — динамика по годам"}
    assert launchd_probe.loaded_labels() >= set(LABELS)
    assert latest_backup.path.suffix == ".enc"
```

- [ ] **Step 2: Run the acceptance test and record which external preconditions are not yet satisfied**

Run: `uv run pytest tests/acceptance/test_job_zero.py -q`

Expected before operational setup: FAIL only for absent live OAuth authorization, unprovisioned Metabase, unloaded LaunchAgents or absent backup; schema/data privacy assertions pass.

- [ ] **Step 3: Create a strict acceptance script covering all 14 design criteria**

```bash
# scripts/acceptance-0.1.sh
#!/bin/zsh
set -euo pipefail
umask 077
cd "${0:A:h}/.."
uv run pytest -q
uv run ruff check .
uv run mypy src
uv run alembic upgrade head
uv run health-hub sync medical --dry-run
uv run health-hub sync all --mode incremental
uv run health-hub publish sheets
uv run health-hub dashboard provision
uv run health-hub dashboard provision
uv run health-hub status --json | uv run python -m json.tool >/dev/null
uv run health-hub operations status
archive="$(uv run health-hub backup create --print-path)"
uv run health-hub backup restore "$archive" --target-database health_hub_restore_acceptance --replace
uv run pytest tests/acceptance/test_job_zero.py -q
```

The matching acceptance test also asserts: repeated synthetic import/sync creates no duplicates; a `needs_review` result is excluded then appears only after versioned correction; WHOOP/Oura raw records identify official API origins; COROS records identify official MCP or `official_fit_export`; the Sheets workbook has four required tabs; each dashboard blood point has `source_url`; one failed fake connector does not prevent another from succeeding; stored OAuth grants contain no private API scope.

- [ ] **Step 4: Write exact setup, daily-use and incident procedures**

`docs/runbooks/operations.md` contains these executable sections and commands:

1. Prerequisites: Docker Desktop running, `brew install postgresql@16`, `uv sync`, OAuth profiles from Plans 1–2.
2. First start: `docker compose up -d postgres`, `uv run alembic upgrade head`, `uv run health-hub dashboard prepare`, `export METABASE_APP_DB_PASSWORD="$(<data/secrets/metabase-app-db-password)"`, `docker compose up -d metabase`, `unset METABASE_APP_DB_PASSWORD`, then `uv run health-hub dashboard provision`.
3. Live URLs: Metabase only at `http://127.0.0.1:33000`; PostgreSQL only at `127.0.0.1:55432`.
4. Initial backfill: `health-hub sync medical --dry-run`, `health-hub sync medical --apply`, `health-hub sync all --mode backfill`, review queue, Sheets publish.
5. Indicator selection: exact SQL `UPDATE dashboard_lab_selection SET enabled = false WHERE canonical_code = 'prolactin';`, then `health-hub dashboard provision`; reenabling uses `true`.
6. Scheduling: install, status, `launchctl kickstart`, and uninstall commands from Task 7.
7. Source incidents: interpret `healthy`, `running`, `stale`, `partial`, `failed`, `never_synced`, `disconnected`; reauthorize only the named connector; run that connector then `status`.
8. Review incidents: never promote ambiguous values through SQL; correct them through the Sheets review flow and republish.
9. Metabase recovery: rerun `dashboard provision`; app-owned entities reconcile, user-owned cards remain untouched.
10. Backup: create, locate latest, verify permissions `stat -f '%Sp'`, retention count, `data/secrets/backup-encryption-key` mode `0600`, and make one offline copy of that key outside the incoming Drive archive without printing its content.
11. Restore drill: restore only to `health_hub_restore_YYYYMM`, query three views, compare table counts, then explicitly drop only that named disposable database.
12. Logs: location `data/logs/health-hub.jsonl`, allowed keys, rotation policy, and instruction never to paste raw medical/API content into an incident log.
13. Drive guarantee: inspect OAuth scope equals `drive.readonly`; no output workbook resides under source root; no command in this plan invokes a Drive write method.
14. Shutdown/restart: `docker compose stop`, relaunch Docker Desktop, `launchctl kickstart` services, verify `status` and dashboard.

- [ ] **Step 5: Add a compact README operator entrypoint**

```markdown
## Local operation

Personal Health Hub runs only on this Mac. The medical Google Drive folder is a read-only source.

- Dashboard: http://127.0.0.1:33000
- Safe status: `uv run health-hub status`
- Manual sync: `uv run health-hub sync medical --apply && uv run health-hub sync all --mode incremental`
- Backup: `uv run health-hub backup create`
- Full runbook: [docs/runbooks/operations.md](docs/runbooks/operations.md)
```

- [ ] **Step 6: Execute the complete acceptance script and manually inspect both dashboards**

Run: `chmod +x scripts/acceptance-0.1.sh && scripts/acceptance-0.1.sh`

Expected: all commands exit 0; restore report uses only `health_hub_restore_acceptance`; the Drive dry-run performs zero writes.

Open: `http://127.0.0.1:33000`

Expected manual check:

- `Обзор здоровья` shows source freshness plus available sleep, HRV, resting heart rate, recovery/readiness and training-load series; missing fields appear as gaps.
- `Анализы крови — динамика по годам` has one card per enabled indicator, source-specific low/high boundaries, red out-of-range points, period/laboratory filters and a working source link from every real point.
- No `needs_review` result is visible in either dashboard.

- [ ] **Step 7: Scan logs, Git and network bindings for privacy regressions**

Run: `! rg -i '(bearer |access_token|refresh_token|authorization|patient_name|evidence_excerpt)' data/logs && ! git ls-files | rg '(^data/|\.pdf$|\.dump|\.enc$|client_secret)'`

Expected: both negative scans exit 0.

Run: `docker compose ps && lsof -nP -iTCP:33000 -sTCP:LISTEN && lsof -nP -iTCP:55432 -sTCP:LISTEN`

Expected: both listeners are bound to `127.0.0.1`, never `0.0.0.0` or `::`.

- [ ] **Step 8: Commit acceptance and operations documentation**

```bash
git add scripts/acceptance-0.1.sh tests/acceptance docs/runbooks/operations.md README.md
git commit -m "docs: add health hub operations and acceptance runbook"
```

## Final release gate

The implementation worker may tag `v0.1.0` only after:

- `scripts/acceptance-0.1.sh` exits 0 on the user's Mac;
- both dashboard manual checks pass with real imported history;
- a restore into `health_hub_restore_acceptance` succeeds and production `health_hub` remains untouched;
- source status includes Google Drive, WHOOP, Oura and COROS, with any unavailable official COROS route explicitly shown rather than silently omitted;
- `git status --short` is clean and the final secret/privacy scans return no matches.

Then commit/tag without adding runtime artifacts:

```bash
git tag -a v0.1.0 -m "Personal Health Hub 0.1"
git status --short
```
