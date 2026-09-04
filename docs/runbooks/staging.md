# Локальный staging

## TL;DR

Staging нужен для первого реального WHOOP-подключения без риска для основной
локальной базы. У него отдельные Compose project, PostgreSQL, Metabase, порты,
volumes и каталоги данных. Production `.env` не копируется и не загружается.

```bash
uv run health-agent staging start
uv run health-agent staging run -- health-agent whoop status
uv run health-agent staging run -- health-agent dashboard setup
uv run health-agent staging stop
```

`stop` сохраняет volumes и `.staging/`. Для повторного запуска используется тот же
`start`. Удаление только staging-volumes требует точного подтверждения:

```bash
uv run health-agent staging clean --confirm health-agent-staging
```

Даже `clean` не удаляет локальные файлы `.staging/`, включая отдельный credentials
file. Их можно удалить вручную только после проверки точного пути и если staging-
данные больше не нужны.

## Изоляция

| Ресурс | Staging | Production default |
|---|---|---|
| Compose project | `health-agent-staging` | каталог/обычный Compose project |
| PostgreSQL port | `56432` | `55432` |
| Application DB | `health_agent_staging` | `health_agent` |
| Metabase port | `54000` | `53000` |
| Metabase app DB | `metabase_staging` | `metabase` |
| Vault | `.staging/vault` | `data/vault` |
| Temp | `.staging/tmp` | `data/tmp` |
| WHOOP tokens | `.staging/tokens/whoop` | `.tokens/whoop` |
| Connector state | `.staging/connector-state` | `data/connectors` |
| WHOOP app credentials | `.staging/tokens/whoop-client.json` | `.tokens/whoop-client.json` |

Команды staging удаляют унаследованные `DATABASE_URL`, WHOOP client ID/secret и
другие управляемые production-переменные из окружения, затем загружают только
`.env.staging` или безопасный пример `.env.staging.example`. Валидатор прекращает
работу, если порт, база, роль, каталог или credentials file пересекаются с
production. Callback `127.0.0.1:8765` — краткоживущий OAuth-listener, а не Compose-
сервис; staging и production OAuth нельзя запускать одновременно.

Если нужны переопределения, скопируйте только пример конфигурации:

```bash
cp .env.staging.example .env.staging
```

Не копируйте production `.env`. Для live WHOOP acceptance создайте отдельный
обычный файл `.staging/tokens/whoop-client.json` с полями `client_id` и
`client_secret`, выставьте `chmod 600`; staging намеренно откажется читать
production `.tokens/whoop-client.json` или symlink на него.

## Promotion flow

1. **Mocked:** полный pytest/миграционные тесты без аккаунта и секретов.
2. **Staging:** отдельная локальная база; OAuth запрашивает только официальные
   read-scopes WHOOP. Коннектор читает аккаунт WHOOP и пишет лишь в локальный
   staging — удалять или изменять данные в WHOOP он не умеет.
3. **Production:** после успешных callback, full sync, status и Metabase smoke код
   продвигается отдельно. Staging database/tokens не копируются в production;
   владелец проходит OAuth ещё раз уже для production-профиля.

Минимальная staging-приёмка:

```bash
uv run health-agent staging start
uv run health-agent staging run -- alembic current
uv run health-agent staging run -- health-agent whoop status
uv run health-agent staging run -- health-agent dashboard setup
uv run health-agent staging stop
```

До OAuth ожидаемый WHOOP status: база доступна, профиль существует,
`configured=false`, `token=missing`. Реальные WHOOP payloads в mocked и
инфраструктурном smoke не используются.
