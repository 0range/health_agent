# Подтверждённые напоминания

## TL;DR

Напоминание сначала создаётся как неактивное предложение. Оно начинает работать
только после `/reminder_confirm CODE` из личного Telegram привязанного профиля.
Отдельный локальный LaunchAgent проверяет срок раз в минуту; публичный сервер и
LLM для выполнения команд не нужны.

## Подготовка

Примените миграцию и убедитесь, что Telegram-бот уже настроен и профиль привязан:

```bash
uv run alembic upgrade head
uv run health-agent telegram status --profile-id PROFILE_UUID
```

Файл `.env` для фонового процесса должен иметь абсолютный путь и права `0600`:

```bash
chmod 600 /полный/путь/к/health-agent/.env
```

## Создание предложения

Дата без смещения интерпретируется только в явно указанном IANA-часовом поясе.
По умолчанию используется `Europe/Moscow`. Неоднозначное или несуществующее
локальное время при переводе часов отклоняется.

```bash
uv run health-agent reminder propose PROFILE_UUID \
  --title 'Повторить ферритин' \
  --reason 'Врач попросил повторить после курса' \
  --when '2026-10-05T10:00' \
  --timezone Europe/Moscow \
  --source-type doctor_note \
  --source-reference document:UUID
```

Следующий минутный запуск пришлёт предложение в Telegram. До подтверждения оно
не выбирается для отправки по сроку. Telegram-сообщение содержит готовые команды:

- `/reminder_confirm CODE` — активировать;
- `/reminder_cancel CODE` — отменить;
- `/reminder_done CODE` — отметить выполненным;
- `/reminder_snooze CODE 1d` — отложить (поддерживаются `m`, `h`, `d`, `w`, не
  более 365 дней);
- `/reminder_reschedule CODE 2026-10-06T10:30` — назначить новое локальное время
  в сохранённом часовом поясе.

Эти переходы детерминированы и не вызывают OpenAI. Та же операция доступна через
локальные `reminder confirm/snooze/reschedule/complete/cancel` для восстановления.

## Автоматическая отправка на Mac

Сначала можно проверить plist, затем явно установить его:

```bash
uv run health-agent reminder render --env-file /полный/путь/к/health-agent/.env
uv run health-agent reminder install --env-file /полный/путь/к/health-agent/.env
uv run health-agent reminder automation-status --env-file /полный/путь/к/health-agent/.env
```

Управляется только `com.orange.health-agent.reminders`. Он запускает одноразовую
команду `reminder dispatch` каждые 60 секунд. Plist содержит пути, но не токены.
Повторный запуск безопасен: предложение и каждый срок имеют постоянный delivery
key в существующем Telegram-аудите.

Остановка сохраняет файлы, удаление затрагивает только два управляемых plist:

```bash
uv run health-agent reminder stop --env-file /полный/путь/к/health-agent/.env
uv run health-agent reminder remove --env-file /полный/путь/к/health-agent/.env
```

## Проверка и восстановление

```bash
uv run health-agent reminder status PROFILE_UUID
uv run health-agent reminder list PROFILE_UUID
uv run health-agent reminder dispatch --env-file /полный/путь/к/health-agent/.env
```

`status` выводит только счётчики. Если Telegram принял сообщение, а процесс упал
до отметки в PostgreSQL, следующий запуск повторит тот же delivery key, не
отправит дубль и завершит локальное подтверждение. Ошибка одного профиля не
останавливает остальные. Тесты не устанавливают LaunchAgent и не обращаются к
живому Telegram или production-базе.

