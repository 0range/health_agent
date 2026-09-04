# WHOOP: подключение и синхронизация

## TL;DR

Коннектор использует только официальный WHOOP Developer API v2. Код, миграции и
mocked-тесты готовы без доступа к личному аккаунту. Чтобы получить реальные
данные, один раз создайте приложение в WHOOP Developer Dashboard, добавьте точный
redirect URI `http://127.0.0.1:8765/whoop/callback`, перенесите client ID/secret в
локальный `.env` и выполните OAuth-команду ниже.

## Подключить профиль

```bash
uv run alembic upgrade head
uv run health-agent whoop auth --profile-id 00000000-0000-0000-0000-000000000001 --account main
uv run health-agent whoop sync --profile-id 00000000-0000-0000-0000-000000000001 --account main --full
uv run health-agent whoop status --profile-id 00000000-0000-0000-0000-000000000001 --account main
```

Команда `auth` открывает официальный WHOOP OAuth и ждёт callback только на Mac.
Пароль WHOOP приложению не передаётся. Один и тот же набор команд работает для
второго локального профиля с другим `profile-id`; токены и данные физически и
логически разделены.

Без `--full` синхронизация повторно читает последние семь дней от предыдущего
успеха. Это намеренно захватывает поздние изменения WHOOP, но не создаёт дублей.
Вес — текущий снимок на момент получения, а не исторический ряд взвешиваний.

## Что сохраняется

- неизменяемые raw-версии profile, body, cycles, recovery, sleep и workouts;
- текущие profile/body и актуальные версии истории;
- recovery, strain, HRV, resting HR, SpO2, skin temperature, показатели сна и
  тренировок, когда WHOOP действительно их возвращает;
- freshness и безопасный статус синхронизации.

Metabase-ready views: `whoop_daily_health`, `whoop_sleep_history`,
`whoop_workout_history`, `whoop_source_status`. Все содержат `profile_id`.

## Официальные источники, проверенные 4 сентября 2026

- API и scopes: <https://developer.whoop.com/api/>
- OAuth и rotation refresh token: <https://developer.whoop.com/docs/developing/oauth/>
- Пагинация: <https://developer.whoop.com/docs/developing/pagination/>
- Лимиты и `X-RateLimit-*`: <https://developer.whoop.com/docs/developing/rate-limiting/>
- Создание приложения: <https://developer.whoop.com/docs/developing/getting-started/>
