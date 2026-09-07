# Three Finished Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. User explicitly requests parallel delivery; isolate implementation branches rather than serializing independent jobs. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver sleep diary with dialogue memory, a separate food bot, and a training bot with annual/weekly plans and reflection in one iteration.

**Architecture:** Extend the existing local PostgreSQL/Telegram/Yandex installation with a small `pilot` package. Reuse its Telegram authentication, replay and delivery handling. Domain services share one profile-scoped record store and an injected model callable; no new distributed platform or vector database.

**Tech Stack:** Python 3.13+, SQLAlchemy, PostgreSQL, Alembic, existing Telegram transport, httpx and existing Yandex credentials.

## Global Constraints

- Three finished user journeys, not merely a persistent-goals increment.
- Life → year → flexible stage (month/two months/quarter) → week; goals persist independently of conversation history.
- Source messages/photos and model output survive processing failures; unknown nutrients are not zero or precise measurements.
- Distinct food/training bot identities and conversation scopes, shared profile facts/goals only.
- Training plans remain local text; never write plans to COROS, calendars or other external platforms.
- Do not change clinical treatment, invent medical targets, make lifespan promises or diagnose from wearable scores.
- Preserve existing medical documents, WHOOP ingestion and services. No personal records, chat exports or credentials in Git.
- Test with disposable PostgreSQL and fakes before live migration. State precisely which external accounts remain unconnected.

## Approved scope and simplifications

Oura and bedroom devices are not launch gates. WHOOP remains the first sleep source. Use official COROS read tools where authorized; if unavailable, complete the training dialogue with explicitly labelled missing COROS data, not synthetic activity. User must authorize real COROS before declaring that integration live. Food and training need separate BotFather credentials. A missing key is an activation requirement, not permission to call a single bot “three separate bots.” Voice records must be retained; if transcription cannot run, request text once with an honest explanation.

Shared records store JSON payloads with profile, domain, kind, source key, timestamp and UUID. This avoids a migration per feature while leaving originals and derived facts distinguishable. Do not implement the superseded standalone goals plan as a prerequisite platform.

## Shared interfaces (root owns)

Root creates `src/health_agent/pilot/contracts.py`, `storage.py`, `brain.py`, `runtime.py`, `cli.py` and the single migration. Domain workers only create their own package files listed below. Exact contracts:

```python
@dataclass(frozen=True)
class Record:
    id: str
    domain: str
    kind: str
    source_key: str
    at: datetime
    payload: dict[str, Any]

class Store(Protocol):
    def put(self, profile_id: UUID, domain: str, kind: str,
            source_key: str, payload: dict[str, Any], *,
            at: datetime | None = None) -> Record: ...
    def list(self, profile_id: UUID, domain: str,
             kind: str | None = None, *, limit: int = 100) -> list[Record]: ...
    def get(self, profile_id: UUID, record_id: str) -> Record | None: ...
    def patch(self, profile_id: UUID, record_id: str,
              payload: dict[str, Any]) -> Record: ...

class Brain(Protocol):
    def __call__(self, system: str, payload: dict[str, Any], *,
                 image_path: Path | None = None) -> str: ...

@dataclass(frozen=True)
class Attachment:
    path: Path
    media_type: str
    caption: str = ""

@dataclass(frozen=True)
class Notice:
    key: str
    text: str

class Coach(Protocol):
    def handle(self, profile_id: UUID, text: str, *, source_key: str,
               now: datetime, attachment: Attachment | None = None) -> str: ...
    def due(self, profile_id: UUID, now: datetime) -> list[Notice]: ...
```

Store is durable/transactional per call and list returns newest first, with stable UUID tie-breaker. `put` replays an identical source key to the existing record; use `patch` to update derived payload. Source keys are unique per profile+domain+kind. `patch` replaces the full JSON. Cross-profile IDs return missing; database does not expose private records. All times are aware UTC; display Europe/Moscow unless domain settings override. Brain is bound by runtime to authorized profile and domain; calls must carry no credentials. Notice delivery is root-owned: on successful messenger send record `kind=notice`, `source_key=notice.key` in the domain; domain `due` checks existing notices. No silent acknowledgement before delivery.

Goals are `domain=shared, kind=goal` payloads with title/level/parent_id/domain/status and optional period_label/criterion/next_step. Domain services read those records; root owns editing/listing goals and initializing private user-provided goals. Conversation records belong to each service's domain and never become confirmed user facts simply because the model said them.

## Task 1: Sleep diary and continuous dialogue

**Owner files:** `src/health_agent/pilot/sleep.py`, `tests/pilot/test_sleep.py`. Root supplies shared contracts/runtime; do not edit them from the worker branch.

**Deliverable:** `SleepCoach(store, brain, *, health_context=None)` implements `Coach`. Optional `health_context(profile_id, text) -> dict[str, Any]` supplies existing verified medical and WHOOP facts. Handle free questions, short morning entries, optional dreams, morning schedule settings, saved goals and weekly review. Store user text before model invocation; store assistant reply only after obtaining it. Preserve a bounded latest 12 conversation turns and latest 14 diary records in prompts. Reference original entries as user reports, not diagnoses. Explicit diary input `/сон текст` and morning response to an outstanding prompt should save a diary entry without turning every medical question into a diary.

- [ ] Write failing tests for text diary → durable record → reply; second-day continuation includes prior user context; repeated update does not create a second diary entry or second model call after reply exists.
- [ ] Implement local morning prompts, default 09:00 Europe/Moscow, configurable `/утро HH:MM`, `/утро выкл`, and read-only `/дневник`. `due` sends once per local date after the configured hour and before noon; no stale overnight notification. Missing WHOOP never suppresses the subjective diary. Schedule a concise weekly reflection on Sunday evening only when enough actual entries exist; `/итоги` always returns a truthful current summary on demand.
- [ ] Preserve source text/optional dream. Missing voice transcription is explicit and does not fake content; runtime can supply transcribed text later.
- [ ] Prompt output is Russian, phone-readable, normally <=1200 characters: conclusion, one relevant basis, next step or one useful question. Avoid irrelevant lab dumps, invented causal claims and generic source lists. Guard urgent concerns via root Brain boundary. Goals shape feedback but are not evidence of health.
- [ ] Add no-provider fallback preserving diary and giving useful saved-entry/schedule feedback, not fabricated insights. Add tests for skipped morning, no input, paused notifications, timezones, insufficient weekly data, unrelated medical question and other-profile records.
- [ ] Run `uv run pytest tests/pilot/test_sleep.py -q` plus scoped Ruff/mypy. Commit and write a report with exact checks and remaining integration assumptions.

Example test spine (use a local in-memory Store fake implementing the complete contract, not production data):

```python
first = coach.handle(profile_id, "/сон Проснулся разбитым", source_key="u1", now=now)
assert store.list(profile_id, "sleep", "diary")
coach.handle(profile_id, "А что мы вчера решили?", source_key="u2", now=now)
assert "Проснулся разбитым" in json.dumps(brain.calls[-1], ensure_ascii=False)
assert first
```

## Task 2: Food bot with timed reminders and durable meal capture

**Owner files:** `src/health_agent/pilot/food.py`, `tests/pilot/test_food.py`. Root supplies contracts/runtime/photo storage and model callable.

**Deliverable:** `FoodCoach(store, brain)` implements `Coach`. `/ел` or a meal description/photo records actual meal time; supports explicit `HH:MM` and `/время HH:MM` correction for the latest meal. Use a configurable profile setting for morning/lunch/afternoon/dinner plate rules. Default interval target 3.5 hours with allowed window 2.5–4.5, no overnight alerts; `/позже 30`, `/пропустить`, `/напоминания выкл` and `/напоминания вкл`. Do not infer omission of a photo means fasting. `/сегодня` and `/неделя` list useful saved facts and adherence with unknowns disclosed. Source order never replaces actual meal date/time.

- [ ] Write failing tests for recorded meal → due after 3.5h, corrected time cancels old due, photo replay uses one meal, late-night meal does not wake user, skip/pause works, restart reloads schedule.
- [ ] Store meal original/caption/photo path before model call. Model requests structured JSON with `foods`, `portion_estimate`, nullable `kcal`, `protein_g`, `fat_g`, `carbs_g`, `saturated_fat_g`, `fiber_g`, `cholesterol_mg`, `confidence`, `unknowns`, `feedback`. Retain raw model text and parsed output. Validate numbers nonnegative/finite, reject unsupported precision assertions, do not use JSON failure as grounds to discard the meal. Photo without known portion is an estimate. Missing values remain null.
- [ ] Implement feedback: short, neutral, one concrete plate observation and optional change relative to user's selected framework. Do not impose all historical chat restrictions. Current food protocol is supplied through a private `domain=food, kind=settings, source_key=protocol` record by root. Keep meal interval a user plan, not a physiological law; do not claim yolks or dairy are universally forbidden.
- [ ] Determine meal category explicitly or from a labelled heuristic; preserve user correction. Last meal marked dinner ends that day's reminder cycle. A future or implausibly old HH:MM should prompt clarification, not silently reanchor reminders. Notices have a stable key tied to meal ID and current corrected target; check sent notice records and silence settings.
- [ ] Implement model failure feedback: meal/photo saved, analysis unavailable; keep the next reminder working. A later replay may retry incomplete analysis without duplicate meals.
- [ ] Run `uv run pytest tests/pilot/test_food.py -q` plus scoped Ruff/mypy. Commit and report checks.

```python
coach.handle(profile_id, "/ел 12:00 Обед", source_key="meal1", now=noon)
assert not coach.due(profile_id, noon + timedelta(hours=3))
assert coach.due(profile_id, noon + timedelta(hours=3, minutes=30))
coach.handle(profile_id, "/напоминания выкл", source_key="pause1", now=noon)
assert not coach.due(profile_id, noon + timedelta(hours=4))
```

## Task 3: Training annual/week plan, facts and reflection

**Owner files:** `src/health_agent/pilot/training.py`, `src/health_agent/pilot/coros.py`, `tests/pilot/test_training.py`, `tests/pilot/test_coros.py`.

**Deliverable:** `TrainingCoach(store, brain, *, activity_source=None)` implements `Coach`. Optional `activity_source(profile_id, since, until) -> list[dict[str, Any]]` reads training facts. `/год` shows stored annual user goals and approximate events, `/план` proposes a local weekly text plan, `/сохранить план` explicitly accepts the most recent proposal, `/итоги` reflects on actual activities versus accepted plan and symptoms. Free-text dialogue remembers prior training messages. A proposed plan is not an accepted plan or completed activity.

- [ ] Write failing tests for year goals → week proposal → acceptance → actual activities → useful reflection; cross-profile isolation, persistent accepted plan, absent activity data and replay safety.
- [ ] Persist generated proposals, accepted plans, conversations and imported activity facts using Store. Never invent race registrations, exact event dates, pace goals or athlete fitness. With insufficient training history ask one useful question; give a conservative high-level draft, not prescriptive intensity from recovery alone.
- [ ] Implement `CorosReadClient` using documented official MCP OAuth/read operations. Browse official COROS docs; do not invent method names. Keep credentials external and inject transport in tests. Expose only reads, not training write tools. No Partner API approval needed for the documented self-service option, but real OAuth remains required. If full OAuth cannot be completed without user, provide a tested ready-to-authorize adapter and an explicit status; do not substitute WHOOP workouts as if they were COROS.
- [ ] `due` returns one weekly reflection on Sunday evening when actual data or an accepted plan exists; it must mention missing data and not assume missed workouts. `/итоги` works on demand. Annual goals are supplied privately by root, not hard-coded user health data in tests.
- [ ] Verify outgoing fake transport calls contain no write operation. Run scoped tests and lint/type checks, commit and report any remaining OAuth requirement.

```python
proposal = coach.handle(profile_id, "/план", source_key="p1", now=now)
assert proposal and not store.list(profile_id, "training", "accepted_plan")
coach.handle(profile_id, "/сохранить план", source_key="p2", now=now)
assert store.list(profile_id, "training", "accepted_plan")
summary = coach.handle(profile_id, "/итоги", source_key="p3", now=now)
assert summary
assert all("write" not in call for call in activity_transport.calls)
```

## Task 4: Root integration, local activation and acceptance

Root owns `pilot/contracts.py`, `storage.py`, `brain.py`, `runtime.py`, `cli.py`, migration `0015_pilot_records.py`, root CLI registration, existing transport compatibility edits, startup/healthcheck and `tests/pilot/test_storage.py`, `test_runtime.py`, `test_brain.py`, `test_acceptance.py`.

- [ ] Add SQLAlchemy table with profile FK and uniqueness `(profile_id, domain, kind, source_key)`; timestamp indexes, JSONB payload and UUID identity. Register model with Alembic and cleanup fixture. Implement Store methods with short transactions, deterministic order and replay-safe put. Source conflict is not an overwrite; derived updates use patch. Keep failed inputs recoverable.
- [ ] Implement a profile-consented Yandex Brain using configured text model and configured vision model; reuse credentials and existing urgent-question guard. Bound context/output and validate model responses in domain services. Test text and image requests using injected client. Voice transcription uses a supported configured provider only after verification; otherwise retained audio is explicitly marked awaiting transcription.
- [ ] Build pilot runners around existing TelegramUpdateService/TelegramMessenger/SqliteTelegramState. Separate token/state roots for food and training; main sleep uses current identity. Add domain-specific help while preserving default existing help, caption propagation to attachment provenance, permanent private attachment storage, diary/meal routing and replay semantics. For the main bot route medical PDFs to existing medical inbox, not food/sleep processing.
- [ ] A single periodic dispatch path queries coach.due and uses durable messenger delivery keys, records notices only after success, handles rate-limit/retry/unknown-delivery without sending duplicate prompts. Expose `pilot status`, `pilot run`, `pilot configure`, and background installation using existing launchd conventions. Avoid two simultaneous pollers for one bot.
- [ ] Provide local-only credential entry/configuration instructions; never request tokens in chat or print their contents. Confirm owner binding with existing verified identity; a second user's data remains isolated. User may need to press Start in each new bot before messages can be delivered.
- [ ] Add goals list/edit/pause in runtime using shared records; parent links only same-profile and higher-level, flexible periods, no auto parent rewrites. Persist user-approved goals, provisional 2027 races and current food framework in private database/config after migration, with originals labelled user-provided.
- [ ] Test end-to-end fake Telegram update → PostgreSQL → model → reply; restart runner → continue; due notification → once-only delivery; meal correction and timeout; annual/weekly plan → actual activity → reflection; saved goals available across domains while chats remain separate. Run complete existing test suite once after integration, scoped suites during edits, Ruff and mypy.
- [ ] Review each domain diff against its brief and test report, then final combined diff. User-requested parallel branches may be integrated only after conflicts are resolved explicitly. Track findings and fix reviews in the local execution ledger.
- [ ] Back up using existing project procedure before additive live migration, activate only configured accounts, smoke-test real main bot/WHOOP/AI. New bot tokens and COROS OAuth are explicit external activation gates. Do not report all three journeys live until all their required real integrations are verified.
- [ ] Update a short readiness table: scenario, code/tests, live activation, exact owner action. Put nonblocking UX improvements in backlog. Commit code/doc changes, keep private artifacts ignored, leave existing jobs working.
