# Telegram Connector

## TL;DR

Коннектор работает через long polling на локальном Mac, принимает только явно
привязанные личные чаты и всегда передаёт `profile_id` в Health Agent или
медицинский inbox. Текст вопросов и содержимое файлов не сохраняются в его
техническом журнале. Production composition подключена командой `telegram run`:
профильные вопросы, PDF/JPEG/PNG importer и явная проверка распознанных значений.

## Что уже реализовано

- `getUpdates` long polling с bot-scoped offset; при старте `getMe` подтверждает,
  что gateway и durable namespace принадлежат одному боту; webhook не создаётся,
  а настроенный webhook блокирует запуск;
- allowlist `Telegram user_id -> Profile UUID`, один-к-одному, только private chat;
- вопросы с профилем, временем и идентификаторами сообщения направляются в
  `HealthQuestionService`, который также можно вызвать из будущей локальной UI;
- `/help`, `/status`, `/sync` направляются в справку и профильные сервисы; `/sync`
  выводит локальные команды, но не запускает синхронизацию;
- `/review` показывает ровно один неподтверждённый пункт из документов этого
  профиля, бота и чата. `/confirm UUID`, `/correct UUID VALUE UNIT`, `/reject UUID`
  явно применяют решение; свободный текст никогда не меняет факты;
- PDF/document, photo и voice сначала полностью сохраняются в приватный staging,
  ограничиваются 20 МБ, независимо хешируются SHA-256, сверяются по размеру и
  сигнатуре PDF/JPEG/PNG/OGG и лишь затем передаются в `MedicalInbox`; MIME от
  Telegram сохраняется как недоверенный metadata, parser использует validated MIME;
- исходящие ответы и напоминания режутся на части до 4096 символов, повторная
  отправка по ключу `(bot_id, profile_id, delivery_key, part)` подавляется. 429
  откладывает попытку до полного `retry_after`; transport/5xx после `sendMessage`
  никогда автоматически не повторяются и получают видимый `delivery_unknown`;
- update обрабатывается по owner/generation claim с продлеваемой lease и fenced
  completion; временные ошибки имеют сохранённый exponential backoff и не более
  четырёх автономных попыток;
- технический SQLite-журнал хранит только ID, профиль, статусы, размеры и хеши —
  без вопросов, ответов, подписей и байтов вложений.

## Первичная настройка на Mac

1. Создать бота через официальный [@BotFather](https://t.me/botfather), как
   описано в [Telegram Bot Features](https://core.telegram.org/bots/features), и
   считать токен паролем к боту.
2. Проверить токен через `getMe` и сохранить token+bot ID одной атомарной записью,
   без аргумента командной строки и shell history:

   ```bash
   uv run health-agent telegram configure-token
   ```

3. Написать боту `/start` в личном чате, затем локально узнать числовые ID без
   вывода текста сообщения:

   ```bash
   uv run health-agent telegram discover-id
   ```

4. Привязать ID к существующему профилю. Для первого профиля миграция создаёт
   UUID `00000000-0000-0000-0000-000000000001`:

   ```bash
   uv run health-agent telegram bind 00000000-0000-0000-0000-000000000001 123456789
   uv run health-agent telegram status --profile-id 00000000-0000-0000-0000-000000000001
   ```

`discover-id` читает pending updates, но не подтверждает их новым offset. Он не
печатает имя, username или текст. Если у бота уже есть webhook, команда и poller
останавливаются; удаление webhook остаётся явным действием владельца.

Токен по умолчанию лежит в `data/telegram/bot-token` с режимом `0600`, каталог —
`0700`; состояние — в `data/telegram/state.sqlite3`, staging — в
`data/telegram/staging`. Весь `data/` исключён из Git. Пути независимо меняются
через `TELEGRAM_ROOT`, `TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_STATE_FILE` и
`TELEGRAM_STAGING_ROOT`; сам токен не следует класть в `.env`. Существующие
symlink-компоненты, symlink-файлы и не-regular targets отвергаются до открытия.

Для live acceptance следует задать отдельные test token/state/staging paths и
привязать только test chat. Продовый token/state при этом не открываются. Замена
токена другим ботом создаёт новый bot namespace с пустым offset и отдельными
identity/update/delivery keys; старый namespace сохраняется. Старый raw-token
файл требует повторного `configure-token`, а legacy SQLite безопасно переносится
в неактивный bot-0 namespace.

## Контракты приложения

```text
Telegram Bot API
  -> TelegramLongPoller
  -> TelegramUpdateService (allowlist + profile context)
       -> HealthQuestionService.answer(question)
       -> HealthCommandService.status/sync(command)
       -> MedicalInbox.ingest(provenance, byte stream)
  <- TelegramMessenger (answers and reminders)
```

`MedicalInbox` получает уже полностью проверенный и повторно читаемый staging
stream, обязан дочитать его до конца, атомарно commit-ить результат и
дедуплицировать по `(profile_id, source_external_id)`.
Одинаковые байты двух профилей намеренно остаются двумя независимыми входами и
не могут смешиваться. JPEG/PNG поступают в общий vault/importer: оригинальные
байты сохраняются, SHA-256 дедуплицируется внутри профиля, каждое происхождение
остаётся отдельной source link. Импорт изображений ограничен 20 МиБ и 25 млн
пикселей; MIME сверяется с сигнатурой, заголовок проверяется до полного
декодирования, анимационные PNG не принимаются. JPEG должен содержать ровно один
кадр и завершаться своим EOI: добавленные потоки/данные и MPF отклоняются.
Временные файлы имеют режим
0600 и удаляются после успеха/ошибки в работающем процессе. Аварийное завершение
процесса может оставить приватный staging/temp-файл; это не TTL-spool ответов.

На Mac используется локальный Apple Vision через `/usr/bin/swift`, без облачного
OCR, с таймаутом 30 секунд и лимитом 100000 символов. При недоступности/ошибке
OCR оригинал сохраняется с `ocr_required`, без выдуманных кандидатов.
Распознанные значения всегда `needs_review`. Voice пока не транскрибируется.

После загрузки используйте `/review` и сравните значение, единицы и медицинскую
дату с оригиналом. Исправление создаёт новую verified-версию с ссылкой на исходную
строку; исходное распознанное значение не перезаписывается. Повтор того же решения
даёт тот же ответ, конфликтующее решение уже обработанного пункта не применяется.
NaN/Infinity и экстремальные числа отклоняются без изменения пункта. Общие
технические пределы: 64 символа, 28 значащих цифр, десятичная экспонента от -12
до 12 и модуль не более 10^12; это не медицинские референсные диапазоны.
OCR может не создать ни одного кандидата: пустая очередь не подтверждает полноту
распознавания. Недостающую/неверную дату нужно явно исправить локально:

```bash
uv run health-agent review set-date DOCUMENT_UUID --collected-date YYYY-MM-DD --profile-id PROFILE_UUID
uv run health-agent review correct ITEM_UUID --value 42.5 --unit ng/mL --profile-id PROFILE_UUID
```

В v0.1 вес поступает только из WHOOP; ручной Telegram-ввод веса, симптомов,
лекарств и добавок этим срезом не реализован. Обработка исторического Drive/Gmail
backlog через Telegram не включена; пункт доступен только при наличии source link
из текущего бота/чата. Повторная загрузка тех же байтов добавляет provenance,
но не запускает повторный разбор уже сохранённого документа.

Для проактивного напоминания приложение вызывает
`TelegramMessenger.send_to_profile(profile_id, text, delivery_key=...)`.
Стабильный delivery key обязателен.

## Ограничения и честность доставки

Telegram документирует, что `getUpdates` и webhook взаимоисключающие, update
подтверждается offset больше его `update_id`, а updates хранятся не дольше 24
часов. Поэтому Mac должен регулярно работать; offset повышается только после
терминальной обработки. См. официальный [Bot API: getUpdates](https://core.telegram.org/bots/api#getupdates).

Облачный Bot API разрешает `getFile`-скачивание до 20 МБ, а `sendMessage` — текст
от 1 до 4096 символов. В ответе 429 параметр `retry_after` задаёт паузу. См.
[Bot API: getFile](https://core.telegram.org/bots/api#getfile),
[sendMessage](https://core.telegram.org/bots/api#sendmessage) и
[ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

Дубликаты одного update и одного bot/profile delivery key подавляются локально.
Telegram не принимает наш idempotency key для `sendMessage`, поэтому transport
timeout, 5xx и падение после возможного remote acceptance считаются неоднозначной
доставкой: retry не выполняется, запись и update получают `delivery_unknown`.
`telegram status` отдельно показывает `delivery_unknown_count`, verified bot,
webhook state и свежесть heartbeat; наличие файла токена само по себе больше не
означает рабочее соединение.

`retry_after` не обрезается: connector сохраняет точное UTC-время следующей
попытки и засыпает до него. Остальные временные ошибки одного update используют
5/10/20-секундный backoff; четвёртая неудачная попытка терминально становится
`needs_attention`, чтобы агент или Bot API не попали в hot loop.

## Что нужно для живого запуска

После привязки запустите `uv run health-agent telegram run`. Нужны локальная БД с
миграциями, приватный token и настроенный ключ вопросного responder; приложение
проверяет responder configuration при запуске. Синтетический offline suite не
подтверждает наличие Swift/Vision на конкретном Mac и качество OCR фотографий.
Эти пункты и реальную Telegram-доставку проверяет владелец отдельно.

Подготовленные ответы вопросов и команд проверки временно сохраняются в общем
приватном `TELEGRAM_ROOT/prepared-replies` (0700/0600, до 128 КиБ/ответ), включая
показанные медицинские значения. Это не технический SQLite-журнал. Spool
сохраняет точные байты при 429/restart; терминальная обработка удаляет файл,
семидневный TTL очищает старые orphan replies. Crash между DB commit и созданием
spool безопасен для решений: повтор сверяет сохранённый review decision и lineage.
Unknown-send остаётся без автоматического retry, существующий fencing не меняется.

В тестах эти границы полностью замоканы: тестовый suite не обращается к Telegram,
боту, модели или живой медицинской базе.
