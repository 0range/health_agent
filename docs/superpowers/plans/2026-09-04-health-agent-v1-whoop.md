# Health Agent v1 — WHOOP Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подключить один или несколько WHOOP-аккаунтов к локальным профилям, загрузить всю доступную историю и безопасно обновлять её без дублей.

**Architecture:** Узкий клиент официального WHOOP Developer API v2 получает profile/body/cycle/recovery/sleep/workout. OAuth-токены лежат в отдельных защищённых файлах на профиль и аккаунт; PostgreSQL хранит неизменяемые raw-версии и актуальные нормализованные строки, каждая из которых принадлежит ровно одному профилю.

**Tech Stack:** Python 3.13, httpx, SQLAlchemy, Alembic, PostgreSQL 18, Typer, pytest.

## Global Constraints

- Только официальный WHOOP Developer API v2 и OAuth 2.0.
- Scopes: `offline read:profile read:body_measurement read:cycles read:recovery read:sleep read:workout`.
- Один профиль может иметь несколько WHOOP-аккаунтов; токены, raw и normalized данные всегда изолированы по `profile_id` и `connection_id`.
- Access token, refresh token и client secret не попадают в PostgreSQL, Git, stdout, исключения или тестовые fixtures.
- Token directory имеет mode `0700`, token/journal files — `0600`; запись
  атомарная, а DB-coordinated journal восстанавливает прерванную публикацию.
- Backfill идёт до пустого `next_token`; incremental повторно захватывает последние семь дней и остаётся идемпотентным.
- `429` уважает `X-RateLimit-Reset`, а `429`/`5xx`/transport errors повторяются с ограниченным backoff.
- Вес WHOOP — только текущий снимок на время получения, не историческое взвешивание.
- Миграция WHOOP `0005_whoop` следует за foundation-миграцией `0004_chart_integrity`.

---

### Task 1: OAuth and protected per-profile token storage

**Files:**
- Create: `src/health_agent/whoop/oauth.py`
- Create: `src/health_agent/whoop/tokens.py`
- Modify: `src/health_agent/config.py`
- Test: `tests/whoop/test_oauth.py`
- Test: `tests/whoop/test_tokens.py`

**Interfaces:**
- Produces: `WhoopOAuth`, `WhoopToken`, `TokenStore.load(profile_slug, account_name)`, `TokenStore.save(...)`.

- [x] Build an eight-character OAuth state and exact official authorization URL; reject callback state mismatch and OAuth errors.
- [x] Exchange and rotate tokens at the official token endpoint; preserve the newly rotated refresh token.
- [x] Atomically store one token bundle per sanitized profile/account path with directory `0700` and file `0600`.
- [x] Mock every HTTP request and assert secrets never occur in CLI output or exception text.
- [x] Run `uv run pytest tests/whoop/test_oauth.py tests/whoop/test_tokens.py -q` and commit.

### Task 2: Official paginated API client

**Files:**
- Create: `src/health_agent/whoop/client.py`
- Test: `tests/whoop/test_client.py`

**Interfaces:**
- Produces: `WhoopClient.get_object(path)`, `WhoopClient.iter_collection(path, start)`.

- [x] Request the six official v2 resources with Bearer auth and collection limit `25`.
- [x] Follow `next_token` as `nextToken` until absent; reject repeated pagination tokens.
- [x] Retry transport errors, `429`, and `5xx`; use `X-RateLimit-Reset`/`Retry-After` when present and never retry ordinary `4xx`.
- [x] Refresh once on `401`, persist the rotated bundle, then retry the original request once.
- [x] Run `uv run pytest tests/whoop/test_client.py -q` and commit.

### Task 3: Profile-owned raw and normalized schema

**Files:**
- Modify: `src/health_agent/models.py`
- Create: `alembic/versions/0005_whoop.py`
- Modify: `tests/conftest.py`
- Test: `tests/whoop/test_schema.py`

**Interfaces:**
- Produces: `WhoopConnection`, `WhoopRawRecord`, `WhoopProfileCurrent`, `WhoopBodyCurrent`, `WhoopCycle`, `WhoopRecovery`, `WhoopSleep`, `WhoopWorkout`.

- [x] Give every table a required `profile_id`; use composite foreign keys `(profile_id, connection_id)` so a row cannot point across profiles.
- [x] Keep raw identities unique within a profile/connection/resource/external-id/payload-hash and normalized identities unique within profile/connection/external-id.
- [x] Store typed dashboard metrics plus the full official source object in `source_values`.
- [x] Create profile-aware views including daily health, sleep, workouts, body snapshot and source status.
- [x] Test two profiles with overlapping WHOOP IDs and prove queries/upserts do not mix them.
- [x] Apply migration on an empty disposable PostgreSQL and commit.

### Task 4: Normalization and transactional sync

**Files:**
- Create: `src/health_agent/whoop/normalize.py`
- Create: `src/health_agent/whoop/repository.py`
- Create: `src/health_agent/whoop/sync.py`
- Test: `tests/whoop/test_normalize.py`
- Test: `tests/whoop/test_sync.py`

**Interfaces:**
- Produces: `sync_whoop(session, profile, account, client, mode) -> SyncReport`.

- [x] Canonically hash each untouched response object and append it only when payload changes.
- [x] Normalize official profile/body/cycle/recovery/sleep/workout fields; keep unscored rows and nullable score metrics.
- [x] Use local WHOOP offset for the local day and timezone-aware UTC for instants.
- [x] Backfill without `start`; incremental sync starts seven days before the last success.
- [x] Update current normalized rows only for the newest deterministic source revision; advance freshness only after the whole transaction succeeds.
- [x] Test repeated sync, changed/out-of-order revisions, pagination, partial failure rollback, and two-profile isolation; commit.

### Task 5: Operator CLI and truthful runbook

**Files:**
- Modify: `src/health_agent/cli.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/runbooks/whoop.md`
- Test: `tests/whoop/test_cli.py`

**Interfaces:**
- Produces: `health-agent whoop auth|status|sync --profile-id <uuid> --account <name>`.

- [x] `auth` validates the local target, opens the official page, verifies scopes/user, and durably coordinates token publication with database `token_generation`.
- [x] `status` recovers interrupted publication, validates expiry/required scopes, and prints connection/freshness/counts without personal fields or secrets.
- [x] `sync` selects one profile/account and supports `--full`; default is incremental.
- [x] Clearly state that mocked tests do not mean a live WHOOP account is authorized.
- [x] Run CLI tests and commit.

### Task 6: Gates and live-auth handoff

**Files:**
- Modify: `README.md`

**Interfaces:**
- Produces: a connector ready for one user OAuth action.

- [x] Run `uv run pytest -q`, `uv run ruff check .`, and `uv run mypy src`.
- [x] Run auth URL generation and status against an unconnected test profile without real credentials.
- [x] Record exact official documentation references and the one remaining live OAuth action.
- [x] Commit the final connector slice.
