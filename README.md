# Health Agent

Личный Health Agent, который хранит медицинские данные локально на Mac.

## TL;DR

Первый рабочий срез уже принимает PDF, не создает дубли при повторной загрузке,
сохраняет оригинал и происхождение данных, отправляет найденные показатели на
проверку и показывает в Metabase только подтвержденные значения.

Gmail-коннектор реализован и проверен на mocked API и disposable PostgreSQL: PDF
из почты проходит тот же импорт и review, что локальный файл. Для живой почты
нужны Desktop OAuth client и авторизация каждого аккаунта. В режиме Google
External/Testing она истекает через семь дней; для фоновой работы нужен реально
опубликованный Production-проект (либо Internal Workspace).

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

## Gmail

У одного профиля может быть несколько почтовых аккаунтов. Первый запуск смотрит
последние семь дней, дальше использует Gmail history cursor; письма и вложения не
меняются. Неоднозначные PDF классифицируются по содержимому; визиты из тела
письма и файлы, которым нужен OCR, остаются во внутреннем attention-статусе без
лишних вопросов в Telegram. Spam и Trash не импортируются.

```bash
uv run health-agent gmail configure PROFILE_UUID personal
uv run health-agent gmail auth PROFILE_UUID personal
uv run health-agent gmail sync PROFILE_UUID --account-id personal
uv run health-agent gmail status PROFILE_UUID
```

Точная OAuth-настройка и правила классификации описаны в
[инструкции Gmail-коннектора](docs/integrations/gmail.md).
