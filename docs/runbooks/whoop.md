# WHOOP: подключение и синхронизация

## TL;DR

Коннектор использует только официальный WHOOP Developer API v2. Код, миграции и
mocked-тесты готовы без доступа к личному аккаунту. Чтобы получить реальные
данные, один раз создайте приложение в WHOOP Developer Dashboard, добавьте точный
redirect URI `http://127.0.0.1:8765/whoop/callback`, сохраните client ID/secret в
локальный `.tokens/whoop-client.json` с mode `0600` и выполните OAuth-команду ниже.
Переменные `WHOOP_CLIENT_ID` и `WHOOP_CLIENT_SECRET`, если заданы обе, имеют
приоритет над файлом.

## Подключить профиль

```bash
uv run alembic upgrade head
uv run health-agent whoop auth --profile-id 00000000-0000-0000-0000-000000000001 --account main
uv run health-agent whoop sync --profile-id 00000000-0000-0000-0000-000000000001 --account main --full
uv run health-agent whoop status --profile-id 00000000-0000-0000-0000-000000000001 --account main
```

Команда `auth` открывает официальный WHOOP OAuth и ждёт callback только на Mac.
Пароль WHOOP приложению не передаётся. Для второго человека сначала выполните
`uv run health-agent profile create "Имя"`, возьмите напечатанный UUID и
подставьте его в `--profile-id`. Токены и данные профилей физически и логически
разделены.

Без `--full` синхронизация повторно читает последние семь дней от предыдущего
успеха. Это намеренно захватывает поздние изменения WHOOP, но не создаёт дублей.
Вес — текущий снимок на момент получения, а не исторический ряд взвешиваний.
При длинном rate-limit команда не держит транзакцию часами: она завершится со
статусом `deferred` и точным `retry_at`.
Если токен отсутствует, повреждён или потерял обязательные scopes, `status`
возвращает безопасный `auth=reauth_required` без чтения персональных данных.

Для одного WHOOP-аккаунта порядок блокировок всегда один: сначала внешний
account-operation lock, затем token-file и PostgreSQL locks. Незавершённая замена
токена восстанавливается по durable journal и `token_generation` из базы. Даже
ошибка после успешного DB commit не откатывает токен вслепую: журнал разрешается
по фактически записанному generation сразу или при следующем status/sync.

## Что сохраняется

- неизменяемые raw-версии profile, body, cycles, recovery, sleep и workouts;
- текущие profile/body и актуальные версии истории;
- recovery, strain, HRV, resting HR, SpO2, skin temperature, показатели сна и
  тренировок, когда WHOOP действительно их возвращает;
- freshness и безопасный статус синхронизации.

Metabase-ready views: `whoop_daily_health`, `whoop_sleep_history`,
`whoop_workout_history`, `whoop_body_snapshot`, `whoop_source_status`. Все
содержат `profile_id`; status-view также показывает число recovery-записей.

Создать или восстановить отдельный WHOOP-дашборд выбранного локального профиля:

```bash
uv run health-agent dashboard setup-whoop --profile-id PROFILE_UUID
```

Команда отклоняет неизвестный профиль и использует его полный UUID во внутренних
именах Metabase, поэтому данные двух людей не могут попасть в одни карточки.

`alembic downgrade` намеренно остановится, если в любой WHOOP-таблице уже есть
данные: сначала нужен явный экспорт или удаление, чтобы история не исчезла молча.

## Pre-release migration lineage

Исправленная `0005_whoop` не была смержена или установлена на пользовательскую
базу; прежняя версия применялась только к одноразовым review-БД. Перед первым live
OAuth база должна обновляться напрямую с `0004_chart_integrity` командой
`uv run alembic upgrade head`. Финальный fingerprint автоматически проверяется
тестом: в `whoop_connections` есть `token_generation`/`retry_at`, в
`whoop_sync_runs` — `retry_at`, а normalized-таблицы имеют `resource_kind` и
`source_values`. Если где-либо сохранилась старая review-БД, помеченная `0005`, её
нельзя использовать: после подтверждения отсутствия реальных WHOOP-данных её нужно
пересоздать, а не пытаться молча продолжить миграцию.

## Официальные источники, проверенные 4 сентября 2026

- API и scopes: <https://developer.whoop.com/api/>
- OAuth и rotation refresh token: <https://developer.whoop.com/docs/developing/oauth/>
- Пагинация: <https://developer.whoop.com/docs/developing/pagination/>
- Лимиты и `X-RateLimit-*`: <https://developer.whoop.com/docs/developing/rate-limiting/>
- Создание приложения: <https://developer.whoop.com/docs/developing/getting-started/>
