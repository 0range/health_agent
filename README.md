# Health Agent

Личный Health Agent, который хранит медицинские данные локально на Mac.

## TL;DR

Первый рабочий срез уже принимает PDF, не создает дубли при повторной загрузке,
сохраняет оригинал и происхождение данных, отправляет найденные показатели на
проверку и показывает в Metabase только подтвержденные значения.

WHOOP-коннектор готов к локальной OAuth-авторизации и проверен на синтетических
ответах API; реальные данные появятся только после однократного входа владельца
WHOOP-аккаунта.

Gmail-коннектор реализован и проверен на mocked API и disposable PostgreSQL: PDF
из почты проходит тот же импорт и review, что локальный файл. Для живой почты
нужны Desktop OAuth client и авторизация каждого аккаунта. В режиме Google
External/Testing она истекает через семь дней; для фоновой работы нужен реально
опубликованный Production-проект (либо Internal Workspace).

Google Drive-коннектор проходит общий медицинский импорт и проверен на mocked API
и disposable PostgreSQL; перед реальными данными остаётся OAuth и live smoke.
Google Sheets создаёт одну понятную таблицу на профиль: подтверждённая история
анализов, пакетная проверка сомнений и свежесть источников. PostgreSQL остаётся
источником истины, а решения из таблицы возвращаются с audit trail.
Telegram уже соединён с профильно-изолированным контуром вопросов на проверенных
анализах и нормализованных WHOOP-данных; живой BotFather/OpenAI запуск всё ещё
требует отдельной проверки владельцем без чувствительных данных.

## Локальная панель управления

После запуска базы панель доступна только на этом Mac по адресу
`http://127.0.0.1:8766`:

```bash
uv run health-agent panel serve
```

Она создаёт локальные профили и показывает безопасный статус WHOOP, Gmail и
Telegram вместе с подсказками для соответствующих CLI-команд. Панель не открывает
браузер, не запускает OAuth и не выполняет sync. Карточка Google Drive в панели
остаётся информационной; рабочий Drive-коннектор настраивается и запускается
отдельными CLI-командами ниже.
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
через встроенные действия `review list`, `review approve` и `review reject`.
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

Первое реальное подключение запускается в полностью отдельном локальном
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
может получить ответ только по проверенным анализам и нормализованным WHOOP-данным
привязанного профиля; ответ всегда содержит локально сформированный список
источников. Настройка API-ключа OpenAI, границы передаваемых данных и обязательная
live-only проверка описаны в [вопросах Health Agent](docs/health-questions.md).

```bash
uv run health-agent telegram configure-token
uv run health-agent telegram discover-id
uv run health-agent telegram bind PROFILE_UUID TELEGRAM_USER_ID
uv run health-agent telegram status --profile-id PROFILE_UUID
uv run health-agent telegram run
```

Границы и подключение описаны в
[инструкции Telegram-коннектора](docs/integrations/telegram.md).

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
WHOOP, Gmail, Google Drive и Google Sheets. Ошибка или 30-минутный таймаут одного источника не
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
