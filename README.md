# Health Agent

Личный Health Agent, который хранит медицинские данные локально на Mac.

## TL;DR

Первый рабочий срез уже принимает PDF, не создает дубли при повторной загрузке,
сохраняет оригинал и происхождение данных, отправляет найденные показатели на
проверку и показывает в Metabase только подтвержденные значения.

Gmail-коннектор уже реализован и проверен на mocked API; для живой почты ему
нужен один Desktop OAuth client и однократная авторизация аккаунта. Google Drive,
WHOOP и Telegram собираются отдельными параллельными срезами.

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
меняются. Неоднозначные вложения остаются во внутреннем статусе и не требуют
подтверждения в Telegram.

```bash
uv run health-agent gmail configure PROFILE_UUID personal
uv run health-agent gmail auth PROFILE_UUID personal
uv run health-agent gmail sync PROFILE_UUID personal
```

Точная OAuth-настройка и правила классификации описаны в
[инструкции Gmail-коннектора](docs/integrations/gmail.md).
