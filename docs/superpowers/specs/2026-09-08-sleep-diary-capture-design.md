# Free-text sleep diary completion

The already agreed sleep job accepts ordinary text without commands. A confirmed live defect saved a morning self-report only as conversation because no morning notice had been delivered. The user explicitly requests finishing this behavior without another planning pause.

Use a conservative local self-report predicate alongside the existing explicit `/сон` and morning-reply paths. Save concrete personal sleep/waking reports; questions, commands and general statements are not diary entries. Keep commands as optional shortcuts. A durable diary write yields an application-authored `Запись сна сохранена.` confirmation, even if the model fails. History and original timestamps remain intact; replay is idempotent. Root reconciles the one already received self-report from its stored turn without resending the Telegram event.

Alternatives: requiring `/сон` adds user work and contradicts the agreed interface; using a second model call for every classification adds latency and failure modes. Choose the bounded local predicate for this pilot. Ambiguous reports remain conversation rather than invented diary facts. No schema, bot, scheduling or model replacement.
