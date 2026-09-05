# Google Sheets для Health Agent

## Что получается

У каждого локального профиля одна отдельная приватная Google-таблица:

- `Lab history` — полная подтверждённая история лабораторных показателей;
- `Needs review` — сомнительные распознавания и четыре поля для решения;
- `Sources` — безопасный статус и свежесть WHOOP, Drive, Gmail и Sheets;
- `_HealthAgent` — скрытая техническая привязка таблицы к профилю.

Таблица — удобное представление. Исходной базой остаётся локальный PostgreSQL.
Google Drive остаётся read-only источником медицинских файлов.

## Подготовка Google Cloud

В том же Google Cloud project включите Google Sheets API и Google Drive API.
Подходит существующий Desktop OAuth client JSON:
`data/secrets/google-oauth-client.json`. Файл должен быть обычным файлом с правами
`0600`; его содержимое нельзя коммитить или отправлять в чат.

Sheets получает отдельный токен только с двумя правами:

- `https://www.googleapis.com/auth/spreadsheets`;
- `https://www.googleapis.com/auth/drive.file`.

Read-only токен архивного Drive-коннектора не превращается в write-токен. Если
Drive уже авторизован, `configure` берёт его проверенную идентичность как ожидаемый
Google-аккаунт. Выбор другого аккаунта во время Sheets OAuth будет отклонён.

## Настройка и запуск

```bash
chmod 600 data/secrets/google-oauth-client.json
uv run health-agent sheets configure PROFILE_UUID
uv run health-agent sheets authorize PROFILE_UUID
uv run health-agent sheets sync PROFILE_UUID
uv run health-agent sheets status PROFILE_UUID
```

Первый `sync` создаёт таблицу. Повторный sync сначала забирает решения, затем
обновляет все управляемые листы. Команда безопасна для повторного запуска.

В `Needs review` допустимы решения:

- `approve` — принять распознанное значение без исправлений;
- `correct` — обязательно указать `Corrected value`, при необходимости unit и
  canonical name;
- `reject` — отклонить строку без исправлений.

Не меняйте ID, profile ID, row version и исходные колонки. Повреждённая,
дублированная или устаревшая строка остановит весь пакет до изменения базы.
Ни одно частичное решение при этом не применится.

## Автоматизация и восстановление

После конфигурации основной LaunchAgent обнаруживает Sheets автоматически. Пока
OAuth не выполнен, job имеет статус `deferred` и не мешает WHOOP/Drive/Gmail.

Если запись в Google временно упала после принятия решения, решение уже хранится
в PostgreSQL с provenance и audit. Следующий sync распознает повтор и восстановит
таблицу. `workbook_mismatch`, `account_mismatch` и `review_grid_invalid` требуют
проверки привязки или изменённых технических колонок; токены и содержимое ячеек в
CLI-ошибках не печатаются.

Локальные настройки находятся в `data/google-sheets/PROFILE_UUID/`, исключены из
Git, имеют права `0700/0600` и разделены по профилям. Таблица не получает PDF,
текст страниц, evidence excerpt, vault path, тела писем, API payload или токены.

## Тесты без реальных данных

```bash
uv run pytest tests/google_sheets -q
```

Тесты используют fake Google gateway и disposable PostgreSQL. Они не открывают
OAuth, не вызывают Google API и не читают рабочие медицинские данные.
