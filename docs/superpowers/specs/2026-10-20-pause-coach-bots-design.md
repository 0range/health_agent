# Pause unused coach bots

The owner explicitly requested disabling the food and training Telegram bots,
disabling automatic morning sleep questions, and keeping the main bot available
for questions. This authorizes the reversible operational changes below.

Use launchd's persistent disabled state and unload the two pilot jobs. Preserve
their credentials, histories and installed plists so re-enabling remains possible.
Use the existing `/утро выкл` schedule handler for the main bot. Other main-bot
features, including manual questions, body reminders and weekly reports, retain
their existing configuration; the request only disables morning sleep questions.

The training process currently also runs daily COROS ingestion. Retain that
previously authorized collection independently: expose its existing bounded,
locked, retry-aware `maybe_coros_sync` through `pilot coros-sync --automatic` and
install an hourly one-shot LaunchAgent. No Telegram gateway or model is invoked
by this command. The normal explicit date-range sync keeps its current behavior.
WHOOP, room sensor and weather jobs are independent and remain enabled.

Alternatives considered: stopping only notifications would leave the two requested
bots running; stopping the training process alone would also remove COROS
collection. Separate collection preserves the intended data gathering.

Verify persistent disable flags, absence of the two processes, the stored sleep
schedule and absence of tomorrow's morning notice. Verify fresh main-bot polling
and a synthetic OpenAI request without sending a Telegram message or storing fake
health observations. Report existing source failures separately from this change.
Keep live evidence under ignored, private `data/verification/`.
