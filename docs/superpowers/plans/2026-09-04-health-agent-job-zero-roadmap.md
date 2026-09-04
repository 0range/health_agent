# Health Agent Job 0 Implementation Roadmap

> **Status:** Superseded by the full-product v1 scope; do not execute.

> **Authoritative execution plan:** [Lean v0.1](./2026-09-04-health-agent-lean-v0.1.md). The three detailed plans below are reference material, not required scope.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать на Mac надежный фундамент Personal Health Hub: медицинский архив, wearable-интеграции, проверяемые данные, Google Sheets и локальные дашборды.

**Architecture:** Работа разбита на три последовательных плана. Первый создает проект, базу и медицинский импорт; второй подключает WHOOP, Oura и COROS; третий добавляет пользовательские витрины, расписание, наблюдаемость и восстановление из резервной копии.

**Tech Stack:** Python 3.12, uv, PostgreSQL 16, SQLAlchemy 2, Alembic, Google Drive/Sheets APIs, PyMuPDF, OCRmyPDF/Tesseract, Metabase 0.63.16, Docker Compose и launchd.

## Global Constraints

- Все файлы проекта находятся внутри `health-agent/`.
- Входящая папка Google Drive является read-only источником и не определяет внутреннюю схему данных.
- Никакой компонент не изменяет, не перемещает и не удаляет входящие файлы Drive.
- PostgreSQL и полные wearable-данные остаются локальными и не публикуются в интернет.
- OAuth используется вместо паролей; токены хранятся локально вне Git в файлах с правами `0600`.
- Приватные и reverse-engineered API устройств запрещены.
- Сомнительные лабораторные значения не попадают в графики до ручного подтверждения.
- Для каждого медицинского значения сохраняется ссылка на файл и страницу источника.
- Реальные медицинские документы и токены запрещено добавлять в Git и тестовые fixtures.
- Каждый импорт идемпотентен и оставляет журнал запуска.

---

## Последовательность

1. [Core and Medical Import Plan](./2026-09-04-health-agent-core-medical-import.md)
   - локальный проект и PostgreSQL;
   - read-only загрузка Drive;
   - PDF/OCR;
   - лабораторные показатели;
   - очередь ручной проверки;
   - отдельная Google-таблица.

2. [Wearable Connectors Plan](./2026-09-04-health-agent-wearable-connectors.md)
   - общий контракт коннектора;
   - WHOOP OAuth и backfill;
   - Oura OAuth и backfill;
   - проверка официального COROS MCP и FIT fallback;
   - регулярная синхронизация без дублей.

3. [Views and Operations Plan](./2026-09-04-health-agent-views-operations.md)
   - SQL-витрины;
   - Metabase: обзор и анализы крови;
   - launchd;
   - статусы и журнал ошибок;
   - шифрованные backup/restore;
   - сквозная приемка 0.1.

## Факт первичной миграции

На момент планирования во входящей папке обнаружено 122 PDF за 2017–2026 годы. Среди них есть лабораторные анализы, МРТ/КТ/рентген, УЗИ/эхоКГ, ЭКГ/Холтер/СМАД, эндоскопия/гистология, консультации и комплексные чекапы. Шесть PDF находятся в пользовательской зоне дубликатов.

Эти данные определяют объем первой миграции и набор приемочных примеров, но не создают зависимость от текущих имен папок. Для автоматических тестов используются только синтетические обезличенные fixtures.

## Review gates

- После плана 1: пользователь может увидеть импортированные документы, историю анализов и очередь сомнений в отдельной Google-таблице.
- После плана 2: в базе есть backfill и повторяемая синхронизация всех трех wearable-источников либо официальный FIT fallback для COROS.
- После плана 3: дашборды, автозапуск, статусы и восстановление из backup проходят сквозную приемку.
