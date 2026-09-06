# Health Agent

Личный Health Agent, который хранит медицинские данные локально на Mac.

## TL;DR

v0.1 объединяет локальную базу PostgreSQL, архив медицинских документов,
WHOOP, чтение Gmail/выбранных папок Drive, таблицу анализов в Google Sheets,
дашборды и Telegram. Вопросы используют WHOOP, подтверждённые анализы и отдельно
помеченные выдержки из заключений/записанные ответы врача. Для AI поддержан Yandex;
оплаченный OpenAI не обязателен.

Визиты, вопросы врачу и повторяющиеся напоминания сохраняются локально.
Публикация визита в Google Calendar требует отдельной авторизации и явного выбора;
в календарь попадают вопросы, но не значения анализов или записанные ответы.
У каждого человека собственные подключения и данные.

Импорт файла не означает, что все его показатели распознаны. Неоднозначные строки
не становятся медицинскими фактами; графики строятся только по подтверждённым,
датированным результатам с источником и единицей измерения. Оригиналы сохраняются.
Текущая готовность установленной системы и оставшиеся ограничения — в коротком
[статусе сборки](docs/superpowers/reports/2026-09-06-v01-completion-status.md).
Пять проверяемых сценариев и границы тестирования — в
[приёмке v0.1](docs/v01-acceptance.md).

## Локальная панель управления

После запуска базы панель доступна только на этом Mac по адресу
`http://127.0.0.1:8766`:

```bash
uv run health-agent panel serve
```

Для автоматического запуска при входе в macOS:

```bash
uv run health-agent panel install --env-file /absolute/path/to/health-agent/.env
```

Mac должен быть включён, Docker запущен. Во сне Mac фоновые задачи не выполняются;
наличие установленного LaunchAgent само по себе не гарантирует доступность базы.

Она создаёт локальные профили, позволяет задать папки Google Drive и показывает
статусы WHOOP, Drive, Gmail, Sheets, Telegram, напоминаний и базы. Панель не
запускает OAuth или синхронизацию при чтении страницы.

На `http://127.0.0.1:8766/healthcheck` — общая [проверка состояния](docs/healthcheck.md)
по каждому профилю: даты фактических данных, последние синхронизации и очередь
обработки анализов. Подключение и наличие свежих данных показаны раздельно.
Подробные ограничения и команды: [локальная панель](docs/management-panel.md).

## Три команды

1. Запустить базу, применить схему и подготовить дашборд:

   ```bash
   docker compose up -d --wait && uv run alembic upgrade head && uv run health-agent dashboard setup
   ```

2. Импортировать PDF:

   ```bash
   uv run health-agent import-file /полный/путь/к/анализу.pdf --collected-date 2026-09-04
   ```

3. Открыть именно подготовленный дашборд:

   ```bash
   open "$(uv run health-agent dashboard setup | sed -E 's/.* url=([^ ]+) .*/\1/')"
   ```

   Команда безопасно повторяет настройку и берет точный URL дашборда из ответа.

До ручного подтверждения показатель не появляется на графике. Очередь доступна
через встроенные действия `review list`, `review approve`, `review correct` и
`review reject`. В привязанном Telegram-чате `/review` показывает один пункт,
`/confirm UUID`, `/correct UUID VALUE UNIT`, `/reject UUID` явно применяют решение.
Фото распознаются локально на Mac (Apple Vision); при недоступном OCR сохраняется
оригинал с честным `ocr_required`. Вес v0.1 берётся только из WHOOP.
Дата в команде — реальная дата забора материала; без медицинской даты значение
сохраняется, но намеренно не попадает на график. Для чистого checkout первая
команда использует явный локальный пароль по умолчанию; файл `.env` нужен только
для переопределения настроек и никогда не перезаписывается автоматически.
Исходные медицинские файлы и база находятся только в локальных каталогах,
исключенных из Git.

Подробности: [дизайн первой версии](docs/superpowers/specs/2026-09-04-personal-health-agent-v1-design.md),
[план реализации](docs/superpowers/plans/2026-09-04-health-agent-v1-roadmap.md) и
[бэклог данных](docs/superpowers/specs/2026-09-04-health-data-backlog-design.md).

WHOOP подключается отдельно по короткой
[инструкции](docs/runbooks/whoop.md). Каждый WHOOP-аккаунт принадлежит выбранному
локальному профилю; данные двух людей не смешиваются.

Команда `uv run health-agent dashboard setup-whoop` создаёт или обновляет
[дашборд WHOOP](docs/whoop-dashboard.md): семь отдельных графиков с единицами
и текущий вес с датой получения. OpenAI для графиков не нужен.

Синтетические проверки запускаются в полностью отдельном локальном
[staging-контуре](docs/runbooks/staging.md): `uv run health-agent staging start`.
Обычный `staging stop` сохраняет данные; удаление volumes требует явного точного
подтверждения.

## Gmail

У одного профиля может быть несколько почтовых аккаунтов. Первый запуск смотрит
последние семь дней, дальше использует Gmail history cursor; письма и вложения не
меняются. Неоднозначные PDF классифицируются по содержимому; распознанные визиты
и другие медицинские письма без файла получают минимальную запись-источник в
общей БД без сохранения текста письма. Они и файлы, которым нужен OCR, остаются
во внутреннем attention-статусе без лишних вопросов в Telegram. Spam и Trash не
импортируются, а полный/recovery-скан перепроверяет состояние уже известных
медицинских сообщений.

```bash
uv run health-agent gmail configure PROFILE_UUID personal
uv run health-agent gmail auth PROFILE_UUID personal
uv run health-agent gmail sync PROFILE_UUID --account-id personal
uv run health-agent gmail status PROFILE_UUID
```

Точная OAuth-настройка и правила классификации описаны в
[инструкции Gmail-коннектора](docs/integrations/gmail.md).

## Google Drive

Drive остаётся приватным и только источником: произвольные подпапки и названия
подходят, файлы не перемещаются и не изменяются. PDF попадают в общую БД/review,
сканы — во внутренний attention. Профиль задаётся UUID из локальной БД. Shared
Drive пока явно отклоняется, чтобы не терять изменения из отдельного change log.

```bash
uv run health-agent drive configure PROFILE_ID 'GOOGLE_DRIVE_FOLDER_URL'
uv run health-agent drive auth PROFILE_ID
uv run health-agent drive sync PROFILE_ID
```

## Telegram

Telegram-коннектор принимает только личные чаты явно привязанного пользователя,
изолирует профили и безопасно повторяет обработку после сбоев. Свободный вопрос
получает контекст из проверенных анализов и нормализованных WHOOP-данных
привязанного профиля, отдельно — цитируемые фрагменты заключений и сохранённые
ответы врача. Ответ содержит локально сформированный список источников;
заключение в архиве не считается подтверждением актуального диагноза.
Настройка AI-провайдера и границы передаваемых данных описаны в
[вопросах Health Agent](docs/health-questions.md).

```bash
uv run health-agent telegram configure-token
uv run health-agent telegram discover-id
uv run health-agent telegram bind PROFILE_UUID TELEGRAM_USER_ID
uv run health-agent telegram status --profile-id PROFILE_UUID
uv run health-agent telegram run
```

Границы и подключение описаны в
[инструкции Telegram-коннектора](docs/integrations/telegram.md).

## Напоминания

Напоминание хранится как неактивное предложение, пока владелец профиля явно не
подтвердит его в личном Telegram. После подтверждения отдельный локальный
LaunchAgent проверяет сроки раз в минуту; выполнить, отложить, перенести или
отменить напоминание можно готовой Telegram-командой без обращения к LLM.
Причина и источник сохраняются вместе с полной историей переходов.

```bash
uv run health-agent reminder status PROFILE_UUID
uv run health-agent reminder render --env-file /полный/путь/к/.env
uv run health-agent reminder install --env-file /полный/путь/к/.env
```

Полный сценарий: [подтверждённые напоминания](docs/runbooks/reminders.md).

## Google Sheets

После настройки создаётся одна приватная таблица с листами `Lab history`,
`Needs review` и `Sources`. В `Needs review` изменяются только решение и, при
необходимости, исправленные значение/единица/показатель.

```bash
uv run health-agent sheets configure PROFILE_UUID
uv run health-agent sheets authorize PROFILE_UUID
uv run health-agent sheets sync PROFILE_UUID
uv run health-agent sheets status PROFILE_UUID
```

Точные права OAuth, правила проверки и восстановление описаны в
[инструкции Google Sheets](docs/google-sheets.md).

## Фоновое обновление на Mac

Один локальный LaunchAgent может раз в четыре часа последовательно обновлять
WHOOP, Gmail, Google Drive, извлечение лабораторных кандидатов и Google Sheets. Ошибка или 30-минутный таймаут одного источника не
останавливает следующие. Полная сверка каждого отдельного аккаунта выполняется
при первом запуске и затем раз в семь дней; остальные запуски инкрементальные.

```bash
chmod 600 /полный/путь/к/.env
uv run health-agent automation render --env-file /полный/путь/к/.env
uv run health-agent automation install --env-file /полный/путь/к/.env
uv run health-agent automation status --env-file /полный/путь/к/.env
```

Установка выполняется только явной командой. Подробности, остановка и удаление:
[фоновая автоматизация](docs/runbooks/automation.md).

Извлечение уже импортированных PDF/изображений включается отдельно для профиля:
`uv run health-agent lab-extract configure PROFILE_UUID`. Cloud по умолчанию
выключен; все кандидаты требуют review. Команды, бюджеты, privacy и recovery:
[лабораторный pipeline](docs/lab-extraction.md).
