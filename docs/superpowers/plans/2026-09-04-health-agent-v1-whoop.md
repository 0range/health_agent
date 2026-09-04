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
- Token directory имеет mode `0700`, token file — `0600`; запись атомарная.
- Backfill идёт до пустого `next_token`; incremental повторно захватывает последние семь дней и остаётся идемпотентным.
- `429` уважает `X-RateLimit-Reset`, а `429`/`5xx`/transport errors повторяются с ограниченным backoff.
- Вес WHOOP — только текущий снимок на время получения, не историческое взвешивание.
- Миграция WHOOP должна следовать за отдельной profile-миграцией foundation-ветки; её `down_revision` фиксируется после интеграции этой ветки.

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

- [ ] Build an eight-character OAuth state and exact official authorization URL; reject callback state mismatch and OAuth errors.
- [ ] Exchange and rotate tokens at the official token endpoint; preserve the newly rotated refresh token.
- [ ] Atomically store one token bundle per sanitized profile/account path with directory `0700` and file `0600`.
- [ ] Mock every HTTP request and assert secrets never occur in CLI output or exception text.
- [ ] Run `uv run pytest tests/whoop/test_oauth.py tests/whoop/test_tokens.py -q` and commit.

### Task 2: Official paginated API client

**Files:**
- Create: `src/health_agent/whoop/client.py`
- Test: `tests/whoop/test_client.py`

**Interfaces:**
- Produces: `WhoopClient.get_object(path)`, `WhoopClient.iter_collection(path, start)`.

- [ ] Request the six official v2 resources with Bearer auth and collection limit `25`.
- [ ] Follow `next_token` as `nextToken` until absent; reject repeated pagination tokens.
- [ ] Retry transport errors, `429`, and `5xx`; use `X-RateLimit-Reset`/`Retry-After` when present and never retry ordinary `4xx`.
- [ ] Refresh once on `401`, persist the rotated bundle, then retry the original request once.
- [ ] Run `uv run pytest tests/whoop/test_client.py -q` and commit.

### Task 3: Profile-owned raw and normalized schema

**Files:**
- Modify: `src/health_agent/models.py`
- Create: `alembic/versions/<after_profiles>_whoop.py`
- Modify: `tests/conftest.py`
- Test: `tests/whoop/test_schema.py`

**Interfaces:**
- Produces: `WhoopConnection`, `WhoopRawRecord`, `WhoopProfileCurrent`, `WhoopBodyCurrent`, `WhoopCycle`, `WhoopRecovery`, `WhoopSleep`, `WhoopWorkout`.

- [ ] Give every table a required `profile_id`; use composite foreign keys `(profile_id, connection_id)` so a row cannot point across profiles.
- [ ] Keep raw identities unique within a profile/connection/resource/external-id/payload-hash and normalized identities unique within profile/connection/external-id.
- [ ] Store typed dashboard metrics plus the official source payload fields that do not have typed columns.
- [ ] Create views `whoop_daily_health`, `whoop_sleep_history`, `whoop_workout_history`, `whoop_source_status` including `profile_id`.
- [ ] Test two profiles with overlapping WHOOP IDs and prove queries/upserts do not mix them.
- [ ] Apply migration on an empty disposable PostgreSQL and commit.

### Task 4: Normalization and transactional sync

**Files:**
- Create: `src/health_agent/whoop/normalize.py`
- Create: `src/health_agent/whoop/repository.py`
- Create: `src/health_agent/whoop/sync.py`
- Test: `tests/whoop/test_normalize.py`
- Test: `tests/whoop/test_sync.py`

**Interfaces:**
- Produces: `sync_whoop(session, profile, account, client, mode) -> SyncReport`.

- [ ] Canonically hash each untouched response object and append it only when payload changes.
- [ ] Normalize official profile/body/cycle/recovery/sleep/workout fields; keep unscored rows and nullable score metrics.
- [ ] Use local WHOOP offset for the local day and timezone-aware UTC for instants.
- [ ] Backfill without `start`; incremental sync starts seven days before the last success.
- [ ] Update current normalized rows to the latest raw revision without duplicates; advance freshness only after the whole transaction succeeds.
- [ ] Test repeated sync, changed revisions, pagination, partial failure rollback, and two-profile isolation; commit.

### Task 5: Operator CLI and truthful runbook

**Files:**
- Modify: `src/health_agent/cli.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/runbooks/whoop.md`
- Test: `tests/whoop/test_cli.py`

**Interfaces:**
- Produces: `health-agent whoop auth|status|sync --profile <slug> --account <name>`.

- [ ] `auth` opens the official page and waits on the configured loopback callback, then verifies the returned WHOOP user before marking connected.
- [ ] `status` prints connection/freshness/counts and never prints personal fields or tokens.
- [ ] `sync` selects one profile/account and supports `--full`; default is incremental.
- [ ] Clearly state that mocked tests do not mean a live WHOOP account is authorized.
- [ ] Run CLI tests and commit.

### Task 6: Gates and live-auth handoff

**Files:**
- Modify: `README.md`

**Interfaces:**
- Produces: a connector ready for one user OAuth action.

- [ ] Run `uv run pytest -q`, `uv run ruff check .`, and `uv run mypy src`.
- [ ] Run auth URL generation and status against an unconnected test profile without real credentials.
- [ ] Record exact official documentation references and the one remaining live OAuth action.
- [ ] Commit the final connector slice.
