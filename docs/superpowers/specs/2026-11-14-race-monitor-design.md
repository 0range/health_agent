# Daily race registration monitor

Implementation outcome: after the user provided the prior ChatGPT thread, an
existing enabled cloud task was found. Its Railway relay was repaired and kept
as the sole active scheduler/delivery path. The local implementation below is a
tested fallback and is not installed. See `docs/runbooks/race-monitor.md` for the
active architecture and confirmed source evidence.

User-authorized scope: season 2027, all Running Community 5 km and 10 km races,
IRONSTAR 1/8 only in Sochi/Sirius and Luzhniki. Daily Telegram report at 13:00
Europe/Moscow through the existing verified Training_vitaliy_bot binding.
The user may narrow the watchlist later. Security monitoring has a separate
destination and must not be modified or used by this feature.

Use a separate local Health Agent command and LaunchAgent. This reuses outbound
Telegram delivery and requires no new cloud deployment or proxy. The alternative
of adding a cloud service has an unnecessary operating cost; putting this into
the paused training conversation loop would resume unrelated coaching.

Fetch official Russian HTML using httpx and parse scoped event cards with
Beautiful Soup. Discover Running Community events from its calendar and retain
known annual event pages in a JSON watchlist. Validate the event's own date/year,
distance and actionable registration controls; navigation, account sign-up,
results, an older year's registration, and another distance do not prove that
the target is open. For shared registration pages, check the distance-specific
registration form before reporting an opening. Unknown markup is a check error.
Discover IRONSTAR 1/8 cards by exact type, target year and location; fetch detail
pages when venue or the registration action needs verification.

Persist last successful observations and a daily immutable outbox. Report new
events, dates and registration state changes with official links. When all checks
succeed and nothing changes, send exactly «нет обновлений». Report partial
failures honestly, preserving the previous good observations. Use the existing
bot/profile delivery key mechanism, including rate-limit and ambiguous-send
handling. Never mark ambiguous delivery as success or silently retry it.

Launchd runs a short local gate every minute and on login. The gate uses an
explicit Moscow timezone, permits one daily report after 13:00, and catches up
after sleep. This does not wake a powered-off computer. Private state and logs
live under data/race-monitor. The training conversation poller stays paused.

Validate with fixture tests covering old seasons, false registration controls,
distance selection, location filtering, changed/open/unchanged/error reports,
Moscow scheduling, catch-up and retry/idempotence. Run a live read-only check,
send one setup report through the training bot, then verify launchd installation.
