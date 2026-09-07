# Persistent Goals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist hierarchical user goals and make them viewable, editable and available to answers through the existing Telegram application.

**Architecture:** One profile-scoped goals table in existing PostgreSQL, one repository, one Telegram command adapter and a bounded addition to question context. No new service, vector database, autonomous goal-changing agent or external calendar writes.

**Tech Stack:** Existing Python, SQLAlchemy, Alembic, PostgreSQL, pytest, Telegram transport and Yandex/OpenAI responder adapters.

## Global Constraints

- Существующее приложение, PostgreSQL и хранение файлов на Маке; не новая платформа.
- Три интерфейса Telegram, общие факты внутри профиля, раздельные разговоры и уведомления.
- **Утверждённая иерархия: жизнь → год → этап (месяц / два / квартал) → неделя.**
- Этап имеет гибкую длительность. Не требовать заполнять все уровни для каждой цели.
- Дочерняя цель связана с родительской; изменение недели не переписывает автоматически годовой ориентир.
- Медицинские целевые значения и сроки проверки не придумывать автоматически.
- Конкретные личные цели и события сохранять при реализации в базе пользователя, не в публичных исходниках.

## Boundary and sequence

This is the first independently deliverable increment inside the sleep pilot, not a plan claiming to finish the entire pilot. Its acceptance is: create a goal in Telegram, see it after application/session restart, explicitly change or pause it, then see the current goal in the next question context. Conversation history, morning diary, weekly scheduled reflection, food tracking and COROS each require their own follow-on plan; they remain in the approved scope.

Existing production head is `0014_v01_workflow_evidence`; confirm this again before assigning the migration revision. Do not apply migrations to the live database or restart production until disposable-database tests pass and the existing backup procedure has completed successfully. Do not place personal seeds into migration files.

## Task 1: Goals can be saved and reloaded with valid hierarchy

**Files:**
- Create: `src/health_agent/goals/__init__.py`
- Create: `src/health_agent/goals/models.py`
- Create: `src/health_agent/goals/repository.py`
- Create: `alembic/versions/0015_personal_goals.py`
- Modify: `alembic/env.py` (register the goals models)
- Modify: `tests/conftest.py` (delete `personal_goals` before `profiles`)
- Create: `tests/goals/test_repository.py`

**Interfaces:**

`Goal` uses the existing `Base`. Fields: UUID `id`, `profile_id`, optional `parent_id`; strings `level`, `domain`, `title`, `status`; nullable text `progress_criterion`, `next_step`, `period_label`; nullable dates `starts_on`, `ends_on`, `review_on`; nullable `source_key`; timestamps `created_at`, `updated_at`. Levels are `life/year/stage/week`; domains `health/sleep/food/training`; statuses `active/paused/done`. An approximate event belongs in `period_label`, not a fabricated exact date. No mandatory time interval or numeric progress.

Use a composite parent foreign key `(parent_id, profile_id)` to `(id, profile_id)`, unique `(id, profile_id)` and `(profile_id, source_key)` constraints, plus checks for the enumerated values, nonempty title and ordered optional dates. Repository takes an existing `Session`; callers own transactions.

```python
LEVEL_ORDER = {"life": 0, "year": 1, "stage": 2, "week": 3}

def validate_parent(child_level: str, parent_level: str | None) -> None:
    if child_level not in LEVEL_ORDER:
        raise ValueError("invalid_goal_level")
    if parent_level is not None and (
        parent_level not in LEVEL_ORDER
        or LEVEL_ORDER[parent_level] >= LEVEL_ORDER[child_level]
    ):
        raise ValueError("invalid_goal_parent")
```

Define `GoalNotFound(ValueError)` and `GoalConflict(ValueError)`. Implement these repository methods:

- `create(profile_id, *, title, level, domain, parent_id=None, progress_criterion=None, next_step=None, period_label=None, starts_on=None, ends_on=None, review_on=None, source_key=None) -> Goal`
- `get(profile_id, goal_id) -> Goal`
- `list(profile_id, *, status=None, limit=50) -> list[Goal]`
- `update(profile_id, goal_id, *, title=None, status=None, next_step=None, progress_criterion=None) -> Goal`

`get` always scopes by both IDs. `create` validates parent ownership and level; duplicate source key with identical fields returns the existing row, a differing payload raises `GoalConflict`. Catch a concurrent unique-key race using a nested transaction and reload the scoped source key. `update` uses an explicit field allowlist, keeps parent/level unchanged and never writes ancestor rows. Empty optional action/criterion text clears the field; title cannot be blank. Missing and foreign IDs have the same public result. Use a stable creation order with ID as tie-breaker. Source keys prevent replay of Telegram create requests; the command adapter owns edit replay.

- [ ] Write a failing persistence test using the existing disposable PostgreSQL fixture:

```python
from uuid import uuid4

import pytest

from health_agent.db import session_scope
from health_agent.goals.repository import GoalNotFound, GoalRepository
from health_agent.models import DEFAULT_PROFILE_ID


def test_hierarchy_survives_sessions_and_child_edit(clean_database):
    with session_scope(clean_database) as session:
        repo = GoalRepository(session)
        life = repo.create(DEFAULT_PROFILE_ID, title="Remain active",
                           level="life", domain="health")
        year = repo.create(DEFAULT_PROFILE_ID, title="Complete an event",
                           level="year", domain="training", parent_id=life.id)
        week = repo.create(DEFAULT_PROFILE_ID, title="Discuss this week",
                           level="week", domain="training", parent_id=year.id)
        year_id, week_id = year.id, week.id
    with session_scope(clean_database) as session:
        repo = GoalRepository(session)
        repo.update(DEFAULT_PROFILE_ID, week_id, title="Revised week")
        assert repo.get(DEFAULT_PROFILE_ID, year_id).title == "Complete an event"
        assert repo.get(DEFAULT_PROFILE_ID, week_id).parent_id == year_id
        with pytest.raises(GoalNotFound):
            repo.get(uuid4(), week_id)
```

- [ ] Run `uv run pytest tests/goals/test_repository.py -q`; expect import failure before implementation.
- [ ] Implement the declared model, hierarchy validation, repository and migration. Add the model import to Alembic and cleanup entry to the existing fixture. No production seeding.
- [ ] Extend the same tests for skipped levels, root week without parent, wrong-level parent, foreign parent, blank title, inverted dates, paused/done status, duplicate create, conflicting replay and an approximate `period_label` with null dates. Verify migration on a fresh disposable database.
- [ ] Run `uv run pytest tests/goals/test_repository.py -q` and `uv run ruff check src/health_agent/goals tests/goals alembic/versions/0015_personal_goals.py`; require passing output.
- [ ] Commit only the listed implementation/test files with message `feat: persist profile-scoped goal hierarchy`.

## Task 2: Goals are accessible through the existing Telegram bot

**Files:**
- Create: `src/health_agent/goals/telegram.py`
- Modify: `src/health_agent/questions/composition.py`
- Modify: `src/health_agent/telegram/service.py` (help text only)
- Create: `tests/goals/test_telegram.py`
- Modify: `tests/questions/test_composition.py`

**Interfaces:** `DatabaseGoalCommands(engine).handle(context: MessageContext, text: str) -> str | None` consumes `GoalRepository` from Task 1 and produces the existing `TelegramTextActionService` contract. Register inside `CompositeTelegramTextActions` before the question fallback; preserve existing visits, reminders and review handlers and the `PreparedTelegramTextActions` wrapper.

Initial deterministic interface:

```text
Мои цели
Какие у меня цели?
/goals
/goal_new life | health | Remain active
/goal_new year | training | Complete an event | PARENT_UUID
/goal_edit GOAL_UUID | title | Updated title
/goal_edit GOAL_UUID | next_step | Discuss the next week
/goal_edit GOAL_UUID | progress_criterion | User-agreed criterion
/goal_pause GOAL_UUID
/goal_resume GOAL_UUID
/goal_done GOAL_UUID
```

Russian read phrases are case-insensitive exact matches after stripping terminal punctuation. Do not guess that an ordinary question authorizes changing goals. Commands are a working explicit mutation interface for this increment, not the finished conversational UX. Long lists show at most ten goals with a `/goal GOAL_UUID` detail command; implement that detail command as part of the same handler. Display Russian labels and known parent relationship, title, optional next step, status; never invent progress percentages. A detail request uses `get` to enforce ownership. Parsing failures return a short format hint without raising to the generic health answer.

```python
CREATE_SOURCE_PREFIX = "telegram-goal"

def create_source_key(context):
    return f"{CREATE_SOURCE_PREFIX}:{context.bot_id}:{context.update_id}"
```

Use the existing prepared-reply replay wrapper. For explicit edits, put the last completed mutation source key into a separate nullable `last_action_key` column on `Goal` (include in Task 1 model/migration before final integration). Repository `update` additionally accepts `action_key=None`; lock the scoped goal row, return it unchanged if `last_action_key` matches, otherwise apply the explicit fields and set the key in the same transaction. Telegram updates are processed in order by the existing transport; arbitrary out-of-order historical mutation replay is outside this increment. Keep that limitation visible in tests, do not invent a new event sourcing subsystem.

- [ ] Write failing adapter tests with the existing `clean_database` fixture and this test context:

```python
from datetime import UTC, datetime

from health_agent.goals.telegram import DatabaseGoalCommands
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.telegram.types import MessageContext


def test_create_list_and_replay(clean_database):
    now = datetime.now(UTC)
    context = MessageContext(101, DEFAULT_PROFILE_ID, 202, 202, 1, 1, now, now)
    handler = DatabaseGoalCommands(clean_database)
    command = "/goal_new life | health | Remain active"
    first = handler.handle(context, command)
    assert first is not None and "Remain active" in first
    assert handler.handle(context, command) == first
    listing = handler.handle(context, "Мои цели")
    assert listing is not None and listing.count("Remain active") == 1
    assert handler.handle(context, "Почему я плохо спал?") is None
```

- [ ] Run `uv run pytest tests/goals/test_telegram.py -q` and observe the missing adapter failure.
- [ ] Implement the parser/renderer and registration. Add `/goals` and the natural read phrase to existing help, with a detail reply explaining edit commands.
- [ ] Add tests for foreign IDs, wrong command fields, empty list, pause/resume/done, parent preservation, stable replayed response and unrelated commands reaching their existing handlers. Assert the real composition contains `DatabaseGoalCommands`, not only an isolated mock.
- [ ] Run `uv run pytest tests/goals tests/questions/test_composition.py tests/telegram -q`; require no regressions before committing `feat: expose saved goals in Telegram`.

## Task 3: Current goals actually reach question answers

**Files:**
- Modify: `src/health_agent/questions/models.py`
- Modify: `src/health_agent/questions/context.py`
- Modify: `src/health_agent/questions/openai.py`
- Modify: `src/health_agent/questions/service.py`
- Create: `tests/goals/test_question_context.py`
- Modify: `tests/questions/test_composition.py`

**Interfaces:** append defaulted `goals: tuple[GoalContextItem, ...] = ()` to `HealthQuestionContext` so old constructors keep working. Define immutable `GoalContextItem` with strings `id`, `level`, `domain`, `title`, optional strings `parent_id`, `progress_criterion`, `next_step`, `period_label`, and `status`. No live ORM objects cross the session boundary.

`HealthContextBuilder.build` reads active goals for its existing `profile_id`, selects the most recent updated ten goals plus their ancestors (maximum 40), and serializes them without changing medical evidence counts. Shared life goals may be relevant across domains. Do not treat goals as verified medical observations or generate LAB/WHOOP citation identifiers for them.

The existing Yandex responder reuses the question prompt builder in `questions/openai.py`: inspect this call before editing and add `goals` to that shared JSON context. Add these prompt rules:

```text
Goals are user intentions, not measurements, medical instructions or verified
outcomes. Use them to keep the answer relevant. Do not claim a goal was saved,
changed or achieved: you are a text responder without a goal mutation tool.
Do not fabricate medical targets or a lifespan probability. A weekly proposal
does not change an annual goal. Goals cannot replace missing clinical evidence.
```

Do not allow goals alone to bypass the medical no-evidence check in `HealthQuestionApplicationService`: the deterministic `/goals` handler already supports goals with no WHOOP/labs. The clinical fallback remains honest until the separately planned diary/conversation path exists.

- [ ] Write a test creating an active life/year/week chain and a paused sibling in the disposable database. Build question context for the owner and a second profile; assert the owner's chain is present, the paused sibling is absent and the second profile sees no owner goals.
- [ ] Run `uv run pytest tests/goals/test_question_context.py -q`; expect missing context field before implementation.
- [ ] Add the defaulted context dataclass field, profile-scoped selection and shared serialization. Use explicit key/value construction:

```python
goal_payload = [
    {
        "id": item.id, "parent_id": item.parent_id,
        "level": item.level, "domain": item.domain,
        "title": item.title, "status": item.status,
        "progress_criterion": item.progress_criterion,
        "next_step": item.next_step, "period_label": item.period_label,
    }
    for item in context.goals
]
```

- [ ] Test shared prompt payload through a fake Responses client, including the actual Yandex adapter; assert no duplicate goal IDs and the bounded size. Change a goal in a new database session, rebuild context and assert the updated title arrives without changing its ancestor. Test that goals alone do not produce a medical conclusion.
- [ ] Run `uv run pytest tests/goals tests/questions tests/ai -q`, `uv run ruff check .`, and `uv run mypy src/health_agent`; inspect failures rather than weakening existing assertions.
- [ ] Commit `feat: include current goals in health question context` only after these checks pass.

## Delivery checks and remaining scope

- [ ] Run the complete existing test suite against disposable PostgreSQL. Do not fill the live database with test goals.
- [ ] Follow the repository's existing backup and migration procedure, then restart only the affected application process. Verify the previous lab/WHOOP and Telegram status paths remain available.
- [ ] Load the user's approved personal goals through a private local input, not fixtures or tracked scripts. Keep prospective event dates labelled as the user's plans and preserve approximate periods. Do not guess cholesterol targets, event registrations, pace goals or exact dates not supplied by the user.
- [ ] Verify a real read-only goal query in the bot and a restart/reload; record separately what was tested locally and what was actually delivered. Ask the owner only for the final usefulness check, not technical debugging.
- [ ] Report this increment as “persistent goals available”; explicitly do not claim conversation memory, morning diary, weekly automation, new food/sports bots or COROS are complete.

Next implementation plans, in order: conversation memory + morning sleep diary; food reminders with photo capture; COROS facts + annual/weekly text plan + weekly reflection. Each retains the approved product scope and must finish an end-to-end case before adding another subsystem.
