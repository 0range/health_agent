# Race Monitor Implementation Plan

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

- [ ] Add Beautiful Soup using `uv add beautifulsoup4`.
- [ ] Define `Observation(key, name, url, status, date, registration_url)`.
- [ ] Implement `collect(config, fetch) -> (list[Observation], list[str])`.
- [ ] Scope extraction to `.run-intro__date`, `.run-intro-distances`,
  `.run-intro-botton-buttons` and IRONSTAR `.event-item-wrap`.
- [ ] Verify old-year and wrong-distance forms do not produce `open`.
- [ ] Save the nine running distances and two triathlon location watches.

## Task 2 — Daily state and Training bot delivery

Files: `src/health_agent/races/monitor.py`, `tests/races/test_monitor.py`.

- [ ] Implement `build_report(previous, current, errors)` with exact no-change
  text and registration links on newly open events.
- [ ] Add file lock, immutable daily outbox and atomic private JSON persistence.
- [ ] Implement `python -m health_agent.races.monitor --check` (read-only),
  `--setup` (one setup report), and `--scheduled` (Moscow daily gate).
- [ ] Reuse `PrivateBotTokenStore`, `SqliteTelegramState`, `TelegramBotAPI`,
  `TelegramMessenger.send_to_profile(..., delivery_key=...)` for training only.
- [ ] Test daily gate, changes, errors, state preservation and delivery retry.

## Task 3 — Install and verify

Files: `src/health_agent/races/install.py`, `docs/runbooks/race-monitor.md`.

- [ ] Create a private LaunchAgent invoking the scheduled module every minute,
  with RunAtLoad and explicit working directory; no credentials in plist.
- [ ] Run `uv run pytest tests/races tests/telegram/test_messenger.py` and ruff.
- [ ] Run live `--check`, inspect all source results, then `--setup`.
- [ ] Bootstrap the LaunchAgent; verify it is loaded and duplicate runs skip.
- [ ] Record source findings and installation limitations in the runbook.

The user has explicitly authorized local implementation and delivery. Execute
inline in this session; no further implementation permission is required.
