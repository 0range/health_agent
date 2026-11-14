# Race Monitor Implementation Plan

Final execution adjustment: the user supplied a prior thread with an already
enabled daily cloud task. Keep that task, repair its existing Railway relay,
and do not install a duplicate local LaunchAgent. Local source/runner tests and
the live read-only check are complete; active deployment is documented in
`docs/runbooks/race-monitor.md`.

**Goal:** Send a daily registration digest at 13:00 Moscow through Training bot.

**Architecture:** A standalone module uses official HTML adapters, JSON snapshots
and the existing TelegramMessenger. Launchd invokes an idempotent daily gate.

**Tech Stack:** Python 3.13, httpx, Beautiful Soup, pytest, launchd.

## Global constraints

- Season 2027; Running Community 5/10 km; IRONSTAR 1/8 Sochi/Sirius and Luzhniki.
- Existing verified training bot only; security routing remains untouched.
- Explicit Europe/Moscow timezone; exact no-change message: «нет обновлений».
- Older seasons and ambiguous HTML cannot trigger an open-registration alert.
- HTTP errors must be visible; no duplicate delivery or secret logging.

## Task 1 — Source adapters and watchlist

Files: `src/health_agent/races/sources.py`, `races-2027.json`,
`tests/races/test_sources.py`; dependency in `pyproject.toml` and `uv.lock`.

- [x] Add Beautiful Soup using `uv add beautifulsoup4`.
- [x] Define `Observation(key, name, url, status, date, registration_url)`.
- [x] Implement `collect(config, fetch) -> (list[Observation], list[str])`.
- [x] Scope extraction to `.run-intro__date`, `.run-intro-distances`,
  `.run-intro-botton-buttons` and IRONSTAR `.event-item-wrap`.
- [x] Verify old-year and wrong-distance forms do not produce `open`.
- [x] Save the nine running distances and two triathlon location watches.

## Task 2 — Daily state and Training bot delivery

Files: `src/health_agent/races/monitor.py`, `tests/races/test_monitor.py`.

- [x] Implement `build_report(previous, current, errors)` with exact no-change
  text and registration links on newly open events.
- [x] Add file lock, immutable daily outbox and atomic private JSON persistence.
- [x] Implement `python -m health_agent.races.monitor --check` (read-only),
  `--setup` (one setup report), and `--scheduled` (Moscow daily gate).
- [x] Reuse `PrivateBotTokenStore`, `SqliteTelegramState`, `TelegramBotAPI`,
  `TelegramMessenger.send_to_profile(..., delivery_key=...)` for training only.
- [x] Test daily gate, changes, errors, state preservation and delivery retry.

## Task 3 — Install and verify

Files: `src/health_agent/races/install.py`, `docs/runbooks/race-monitor.md`.

- [x] Prepare the local LaunchAgent installer. Do not activate it: the existing
  cloud task is the sole active schedule.
- [x] Run `uv run pytest tests/races tests/telegram/test_messenger.py` and ruff.
- [x] Run live `--check` and inspect all source results. Send setup confirmation
  via the existing cloud relay; Telegram confirmed `message_id=6`.
- [x] Verify the cloud task is enabled at 13:00 Moscow. Verify both relay
  identities and rejection of cross-channel authorization keys. Verify no local
  race-monitor LaunchAgent is loaded.
- [x] Record source findings and installation limitations in the runbook.

The user has explicitly authorized local implementation and delivery. Execute
inline in this session; no further implementation permission is required.
