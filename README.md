# Health Agent

Личный Health Agent, который хранит медицинские данные локально на Mac.

## TL;DR

Первый рабочий срез уже принимает PDF, не создает дубли при повторной загрузке,
сохраняет оригинал и происхождение данных, отправляет найденные показатели на
проверку и показывает в Metabase только подтвержденные значения.

Google Drive, Gmail, WHOOP и Telegram входят в следующие срезы; README не выдает
их за уже работающие интеграции.

## Три команды

1. Запустить базу, применить схему и подготовить дашборд:

   ```bash
   docker compose up -d --wait && uv run alembic upgrade head && uv run health-agent dashboard setup
   ```

2. Импортировать PDF:

   ```bash
   uv run health-agent import-file /полный/путь/к/анализу.pdf
   ```

3. Открыть Metabase:

   ```bash
   open http://127.0.0.1:53000
   ```

До ручного подтверждения показатель не появляется на графике. Очередь доступна
через встроенные действия `review list`, `review approve` и `review reject`.
Исходные медицинские файлы и база находятся только в локальных каталогах,
исключенных из Git.

Подробности: [дизайн первой версии](docs/superpowers/specs/2026-09-04-personal-health-agent-v1-design.md),
[план реализации](docs/superpowers/plans/2026-09-04-health-agent-v1-roadmap.md) и
[бэклог данных](docs/superpowers/specs/2026-09-04-health-data-backlog-design.md).
