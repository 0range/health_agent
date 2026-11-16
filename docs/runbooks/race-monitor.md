# Race registration monitoring

## Current behavior: quiet monitoring and live card — 16 November 2026

This section supersedes the earlier three-daily-report policy below.
The owner requested silence without updates, a live HTML status card, separate
forwardable opening notices, event dates, and owner-only interaction. They
explicitly confirmed the old training-coach functions are not needed.

- `/status`, `/races`, Russian `статус`: a current HTML card in
  `@Training_vitaliy_bot`, with evidence time, results/errors, dates and buttons.
- `/check` / «Проверить сейчас»: queue a scan, explicitly distinguish queued
  from completed. «Обновить статус» refreshes immediately from stored evidence.
- The existing Railway relay checks for new worker evidence every minute and
  edits only the current card. Its message ID/version survive deployments.
  It sends no new message for unchanged status. Command/menu/webhook access is
  restricted to the fixed owner's sender ID and private chat; foreign input is
  silently ignored. The webhook uses a separate secret header.
- One forwardable HTML message per newly opened event/distance, including a
  verified direct registration URL in the text and a URL button. No private
  callback controls, personal information or content-protection flag. The
  worker persists the exact payload and original event key before sending;
  legacy pending plain-text messages keep their original receipt hash.
- Scheduled checks remain 08:00, 13:00, 18:00 Europe/Moscow; no unchanged
  Telegram or Gmail messages. Sunday 13:00 history/forecast report remains
  mandatory. Hourly/five-minute opening checks remain unchanged.
- Competition dates are separate from opening forecasts. `race_dates` in
  `races-2027.json` contains the official 2027 running calendar and explicitly
  approximate 2026-based triathlon periods. Current event-page dates supersede
  those fallbacks. See `docs/research/race-event-dates-2027.md` for sources.

Cloud implementation: existing relay branch `railway/telegram-notifications`,
commit `b934067`, deployment `0673d338-3d41-4c1f-bed3-7b8dc3954e65`; existing
worker branch `codex/race-cloud-monitor-2027`, commit `40fd78b`, deployment
`6b9547ab-650c-4ff4-ba82-1af8855b020d`. Both deployed successfully. No new service.
The old training poller's last poll was 20 October, before this work; the main
medical bot has a different bot ID and remains independent. Do not start the
old Training long-poller while the cloud webhook owns incoming updates.

Task prompt was independently read back exactly at 13:42:54 Moscow. The only
changed configuration field was `prompt`; schedule, executor, timezone,
enabled state and notification settings remained intact. The backend's next
runs remained 16 November 18:00, 17 November 08:00 and 13:00 Moscow.

Verification: 18 Node 24 relay tests including restart persistence, automatic
in-place refresh, owner checks, HTML/date handling and existing security OAuth;
33 Python race tests including forwardable alerts and legacy retry compatibility;
Ruff passed. A real status card was delivered as `message_id=11`; the owner
confirmed it arrived. Real Telegram webhook requests read worker status and
queued a scan at 13:27:19, completed at 13:27:31 with 11 observations and no
errors. Visible button feedback and a separate card-refresh timestamp were
added after the owner's feedback. See the user guide below for operation.

Final live verification at 13:45 Moscow: the deployed worker exposed all 11
calendar entries. An explicitly internal authenticated handler probe adopted
the already-delivered preview (message 11) as the current card and queued a
scan; it was not presented as a real user click. Worker evidence timestamp was
13:43:50.770238, completion log 13:43:59, 11 observations, zero source errors,
no registration openings. At **13:44:01** the relay logged
`training_card_refreshed`, message 11, with the new evidence timestamp after
Telegram accepted the edit. No new status message was sent. Telegram reported
zero pending webhook updates and no webhook errors. This verifies automatic
in-place refresh on the deployed cloud path, not only a local/unit test.

User guide: [`race-monitor-user-guide.md`](race-monitor-user-guide.md).

## Current race schedule and live verification — 16 November 2026

The user requested checks at **08:00, 13:00 and 18:00 Europe/Moscow**. The existing
task `Automation_ef35471305c4819181e796af6d6a2e2a` remains enabled with executor
`cloud`, source conversation
https://chatgpt.com/c/6af86c1f-1484-83eb-b29c-59501bc6ac18.
No new task, service, local scheduler or Telegram connector was created.

Saved schedule, independently read back at 12:53 Moscow:

```ical
BEGIN:VEVENT
DTSTART;TZID=Europe/Moscow:20261116T130000
RRULE:FREQ=DAILY;BYHOUR=8,13,18;BYMINUTE=0;BYSECOND=0;UNTIL=20271231T205959Z
END:VEVENT
```

The backend's actual next runs were 16 November 13:00, 16 November 18:00 and
17 November 08:00 Moscow. Treat `schedule` and `next_run_times` as authoritative;
the editor/sidebar has displayed inconsistent single-time labels. Do not flatten
the multi-hour RRULE through the single-time editor.

Each slot requests a fresh worker scan and sends a short Training Bot status,
including exactly «Нет обновлений» when fully checked with no new openings.
New openings retain direct registration links. Idempotency keys are now
`races-YYYY-MM-DD-HH00`, using the planned Moscow slot (`0800`, `1300`, `1800`),
including when execution is delayed. Retries keep the original key and text.
This permits three separate daily reports without weakening duplicate protection.
Sunday's history/forecast table replaces only the **13:00** short status; the
08:00 and 18:00 Sunday runs remain ordinary short reports. Immediate opening
alerts, hourly/five-minute windows, history, watched distances, and Gmail fallback
remain intact. The exact prepared prompt edit was verified after the schedule
update; title, model, reasoning effort, cloud executor, timezone, enabled state,
notification flags and conversation ID were preserved.

Live verification from the existing **web race conversation** on 16 November:

- Training readiness confirmed `Training_vitaliy_bot`.
- A forced scan completed at **12:48:44.982990 Moscow**: all 11 observations were
  fresh, zero source errors, no announced season-2027 registrations in the watchlist.
- One explicitly labeled test returned HTTP 200, `ok=true`, `channel=training`,
  **message_id=9**, event `races-cloud-route-test-2026-11-16`.
- Railway independently recorded `/notify/training` HTTP 200 at 09:49:04.679 UTC.
- The race worker remained on deployment `e6d90380-dc00-4db0-b482-cd5a41ef7dd5`,
  with 3600-second cadence and its existing persistent volume. No redeploy needed.

Unlike the tested security conversation's outbound requests earlier that day,
this web race conversation could reach the authenticated HTTP routes. Keep its
working delivery route; do not assume every cloud execution has the same network
behavior. This check verifies the present web path and saved schedule, not a
future unattended execution. Both components run in the cloud; the Mac and this
chat can be closed. The sections below retain setup and diagnostic history.

## Security cloud delivery repaired — 16 November 2026

The 15 and 16 November security tasks completed and their full reports reached
Gmail, but no corresponding `/notify/security` POST reached Railway. The security
source web conversation explicitly tested curl GETs to Telegram, Railway health,
and example.com: all timed out (exit 28, no HTTP response). Local Telegram and
Railway checks returned 302/200 promptly. This points to the cloud environment's
outbound path, not a demonstrated Telegram/Railway outage; its internal cause is
not established. The training task's Sunday 15 November POST returned HTTP 200.

At the user's request, extended the **existing** Railway relay with a private
OAuth-authenticated MCP connection, without adding a service or using Gmail as
a forwarding source. The abandoned empty mail-forwarding service and prototype
were removed; no Gmail credential was deployed. Gmail remains an independent
native connector. The race worker, training route and race task were unchanged.

Private plugin:
https://chatgpt.com/plugins/plugin_asdk_app_6afac74118f881919d35567ea95472e4.
It exposes only `security_delivery_status` and `send_security_notification` to
the fixed owner through `Gccmonitorformebot`. Existing security HTTP calls and
the MCP sender share durable idempotency receipts. Bot tokens stay in Railway;
the scheduled task now contains no plaintext security sender key.

Actual web ChatGPT test at 12:12 Moscow returned `ok=true`, `channel=security`,
`message_id=198`, event ID `safety-mcp-cloud-test-2026-11-16`. Railway recorded
the matching authenticated `/mcp` requests from the OpenAI MCP client. This was
not a local send. Earlier, the user confirmed receipt of the separately labeled
local recovery message 197 with the link to the 16 November morning report.

The security task's delivery section was updated through Scheduled at
12:13:25 Moscow. Fresh readback exactly matched the prepared text and confirmed
unchanged schedule, timezone, enabled state, model, reasoning effort and
notification flags. Research rules, Gmail-first order, alert/silence criteria
and card format were preserved. Next run: **17 November 08:00 Moscow**. The actual
web tool path is verified; the next unattended scheduled execution has not yet
happened and must not be described as already verified.

Relay code/runbook: `0range/personal-assistant`, branch
`railway/telegram-notifications`, `telegram-relay/README.md`. Eight Node 24
integration tests cover authorization/PKCE, consent, token persistence,
refresh/revocation, fixed-recipient delivery, idempotency, upstream failures and
unchanged training key separation. The Railway relay must stay running.

## Weekly history and frequent cloud checks (added 14 November)

The user chose **Sunday 13:00 Europe/Moscow** for the weekly history/forecast
table, replacing that day's ordinary short status. On 14 November the task was
enabled daily at 13:00 (expanded to three daily slots on 16 November, above).
Readback after the final prompt update at 20:34:05 Moscow
confirmed its original RRULE and next runs on 15, 16, 17 November at 13:00.
The editor's secondary label incorrectly displays 11:00; `schedule`,
`default_timezone` and actual `next_run_times` agree on 13:00 Moscow.

Historical evidence and sellout precision are saved in
[`race-registration-history-2027.md`](../research/race-registration-history-2027.md)
and embedded in the task prompt. The White Nights range begins in November
**2026**, despite the target being season 2027. The Sirius November anchor is
only a festival proxy; the exact historical 1/8 opening is unverified.

The user also authorized hourly checks in expected periods and five-minute
checks around a precisely announced opening. A separate Railway service now
owns those checks and immediate training alerts:

- Project/environment: the existing `personal-assistant` / production.
- Service `race-monitor`: `3c754085-e224-41af-b327-e70a55b086f4`.
- Source: `0range/health_agent`, branch `codex/race-cloud-monitor-2027`.
- Source commit: `a58c8f1`; deployment `e6d90380-dc00-4db0-b482-cd5a41ef7dd5`, SUCCESS.
- Dockerfile `deploy/races.Dockerfile`; allowlisted Docker build context.
- One instance, 0.5 vCPU / 0.5 GB configured limit; sleep disabled.
- Durable `/data` volume `race-monitor-state`, ID `e4a2def8-694b-4a35-8efa-ca6e45d6289a`.
- URL: https://race-monitor-production-acc5a209.up.railway.app.
- Only `TRAINING_RELAY_KEY` is supplied, by reference to the existing relay.
  The service has no Telegram bot token, security key or participant credentials.

`race-windows-2027.json` supplies historical windows. Hourly checks apply from
seven days before the earliest point to fourteen days after the latest point;
otherwise the worker checks daily. An explicit official opening enables
five-minute checks from T−30 minutes through T+24 hours. The worker inspects
official event pages and three organizer Telegram channels for unambiguous
dated announcements. Relative dates, image-only or ambiguous announcements
may require the daily task's interpretation; do not assume every format is
machine-readable. Historical estimates never activate an exact opening time.
Checks stop after 31 December 2027.

Authenticated worker endpoints (same narrow training key, never in URL):

- `GET /status`: last check, observations with individual timestamps, errors,
  announced times, immutable outbox and confirmed Telegram message IDs.
- `POST /check`: queues a fresh scan; HTTP 202 is not completion or delivery.
- `POST /openings`: stores `{event_id, opens_at, source}` with a timezone-aware
  timestamp, known event ID and an official source URL. The daily task may add
  a verified announcement; it must not submit a historical estimate.

The daily task requests a fresh scan and consults receipts before reporting a
new opening, retaining its independent official-source check and email fallback.
The worker sends once per season/event/status using immutable messages and the
relay's durable idempotency. A shared event behind login produces a clearly
qualified "check this distance in your account" notice with its direct link,
not a false confirmed opening. A later confirmed opening gets its own alert.

Verification: 36 targeted source/worker/messenger tests passed; lint passed.
The first actual cloud scan at 20:30:45 Moscow found all 11 watches with no
source errors and no open season-2027 registrations. State survived deployment
restarts. An authenticated `/check` subsequently completed a second actual cloud
scan at 20:34:43 Moscow with all 11 watches and no errors. Public health returned
200, unauthenticated status returned 401, and
authenticated status returned the expected 3600-second cadence. Training
readiness confirmed `Training_vitaliy_bot`; the labeled setup message returned
`message_id=7` with idempotency key `races-cloud-monitor-setup-2026-11-14`.
The setup message exercised the same sender/relay path from the local verification
command; cloud scanning was verified separately. A future scheduled Sunday
digest and a real five-minute opening window have not yet occurred.

No local scheduled sender was installed. The security task/service were not
changed by this addition. Both the frequent race worker and daily/weekly task
continue after closing the chat or Mac. Railway `telegram-relay` must stay live.
Participant registration/payment is not automated: no chosen race or price limit
has been authorized.

## Active architecture

The existing ChatGPT task **Регистрация на старты 2027** owns the daily schedule.
ID: `Automation_ef35471305c4819181e796af6d6a2e2a`.
Enabled; Europe/Moscow; 08:00, 13:00 and 18:00 daily through 31 December 2027.
Its first original scheduled run was 15 November 2026 at 13:00.
Its source conversation is
https://chatgpt.com/c/6af86c1f-1484-83eb-b29c-59501bc6ac18.

Notifications use the existing Railway `personal-assistant` production service
`telegram-relay`, source branch `railway/telegram-notifications` at commit
`79ddb52e72403358a5d89eec26cdbc49ef0de1d3`.

- `/notify/training` sends race reports through `Training_vitaliy_bot`.
- `/notify/security` sends security reports through `Gccmonitorformebot`
  (replaced `gpt_mob_monitor_bot` on 14 November after its Telegram API failed).
- Each route has its own existing authorization key and bot credential.
- Keys are retained in private task/service configuration, never in this repo.
- `/ready/{channel}` checks bot identity without sending a message.
- Telegram receipts and idempotency keys live on the Railway volume.

The previous chat had deployed the split-route code and updated both task
prompts, but omitted `SECURITY_BOT_TOKEN` and `TRAINING_BOT_TOKEN`. Both readiness
routes consequently returned HTTP 503 `bot_not_configured`. The old generic
`TELEGRAM_BOT_TOKEN` belongs to `my_assistant_os_bot`, not the security bot; the
service's security identity guard correctly rejected it. This continuation
sets `SECURITY_BOT_TOKEN` to the original security credential recovered from the
authorized security task history and verified by Telegram `getMe`, and
`TRAINING_BOT_TOKEN` to the verified local Training bot credential. Existing
route keys, chat destination and task schedules are preserved.

## Local implementation

The Python source adapters, watchlist and runner were prepared before the user
provided the existing task link. They passed 29 targeted tests and a live
read-only check of all 11 watches, with no source errors. They are available for
manual checks or an explicitly requested later migration:

```sh
.venv/bin/python -m health_agent.races.monitor --check
```

**Do not install the local LaunchAgent while the cloud task is enabled.** No
local race-monitor LaunchAgent is installed by this continuation. The `--setup`,
`--scheduled` and `health_agent.races.install` entry points are a fallback, not
the active schedule. Training conversation polling remains paused independently.

## Coverage and source evidence

Season 2027. All individual 5/10 km Running Community races, including newly
announced matching events; IRONSTAR individual 1/8 only in Sochi/Sirius and
Luzhniki, including partner events. No relays or waiting-list openings.

| Distance | Event | 2027 event dates | Official page |
| --- | --- | --- | --- |
| 5 km | Апрель | 4 April | https://aprilrun5km.runc.run/ |
| 5 km | Московский полумарафон | 17–18 April, exact 5 km day pending | https://moscowhalf5km.runc.run/ |
| 5 km | Красочный забег | 6 June | https://colorrun5km.runc.run/ |
| 5 / 10 km | Большой фестиваль бега | 29 August | https://runfest.runc.run/ |
| 10 km | Ночной забег | 19 June | https://nightrun10km.runc.run/ |
| 10 km | Белые ночи, Санкт-Петербург | 10 July | https://wnmarathon.runc.run/ |
| 10 km | Северная столица, Санкт-Петербург | 8 August | https://spbhalf.runc.run/ |
| 10 km | Московский марафон | 25–26 September, exact 10 km day pending | https://moscowmarathon10km.runc.run/ |
| 1/8 | IRONSTAR Сочи / Сириус; Лужники | Not announced | https://iron-star.com/event |

Checked on 14 November 2026: the running event pages still describe 2026; no
verified open registration for these distances in 2027. IRONSTAR's published
2027 calendar has no matching 1/8 in these two locations. The organizer HAS
announced the event calendar in its official Telegram album:
https://t.me/runc_run/9303 (four images, visually checked). Dates in the table
come from that album, distances from the existing event pages. For the two
weekend events, do not invent the short-distance day before its timetable is
published. The calendar can change. The Student Cup page https://scrc.runc.run/
also lists the April 5 km stage on 4 April 2027. Calendar dates are not evidence
that public registration is open.

The cloud task requests a daily short no-change status and links for new
openings. Unavailable sources must be reported, never called no updates.
It also retains the previously user-authorized email fallback from the linked
conversation. Local and cloud sending must not run simultaneously.

## Delivery verification

The training route accepted a setup message on 14 November 2026:
HTTP 200, `ok=true`, `channel=training`, `message_id=6`, idempotency key
`races-setup-2026-11-14`. No security test message was sent. Scheduled executions
remain owned by the existing ChatGPT task; this verification proves current
delivery readiness, not a future unattended run.

Both `/ready/security` and `/ready/training` subsequently returned HTTP 200
with the intended bot usernames. Each route rejected the other route's key
with HTTP 401. No local race-monitor LaunchAgent is loaded.

### Later security delivery test — 14 November 2026

At the user's explicit request, attempted one test through `/notify/security`
with idempotency key `security-route-test-2026-11-14-user-request-1`.
At 19:41 Moscow, Railway returned HTTP 502 `delivery_unconfirmed` after its
12-second Telegram timeout; no Telegram message ID was received. Do not count
this as successful delivery or resend under another key without resolving
whether the first message arrived. The relay retains ambiguous reservations
to prevent duplicate delivery.

Security readiness also timed out (HTTP 503 `bot_not_verified`); a direct
Telegram `getMe` returned HTTP 502 `Bad Gateway`. Training readiness remained
HTTP 200. The deployment was still successful and unchanged. This is a new
upstream failure after the earlier successful readiness checks; security
delivery is currently unconfirmed. The user was asked whether the test appeared.

The user confirmed that the first test did not arrive. A second authorized
attempt at 19:49 Moscow used
`security-route-test-2026-11-14-user-confirmed-absent-2` and also returned
HTTP 502 `delivery_unconfirmed`, without a message ID. Direct security
`getMe` and `getWebhookInfo` calls both returned Telegram JSON HTTP 502
`Bad Gateway` after approximately 15.5 seconds. A direct training `getMe`
returned HTTP 200 in 0.2 seconds. No deployment or credentials changed.

These observations suggest an upstream problem affecting the security bot;
they do not establish Telegram's internal root cause. Similar first-hand
reports in the official Telegram Bot API issue tracker include
https://github.com/tdlib/telegram-bot-api/issues/933 and
https://github.com/tdlib/telegram-bot-api/issues/939. In the latter, token
rotation did not help and the bot recovered later without an application fix.
Token rotation, webhook changes, or redeployment are not justified by the
current evidence. Retain the existing ambiguous delivery receipts.

An earlier diagnostic incorrectly reported that security Gmail delivery was
disabled. Reading the current task editor and a fresh filtered automations
response showed that Gmail was already allowed for alerts and weekly reports.
The user then explicitly authorized email and a replacement security bot.

Three bounded recovery checks at 19:51:46, 19:52:39, and 19:53:32 Moscow all
returned security readiness HTTP 503 `bot_not_verified`. The recovery probe
exited without sending a third message. No background retry worker was installed.

### Recovery completed — email and replacement bot

The enabled security task `6a927b6f84788191bbbed11efc8123d9` was updated through
the Scheduled task editor at 19:59:51 Moscow. Fresh readback confirmed:

- Same daily 08:00 Moscow schedule and existing alert/weekly/silence criteria.
- Full reports must go to `vitali.arz@gmail.com` before Telegram delivery,
  independently of any Telegram failure or unconfirmed result.
- Delivery status must distinguish email and Telegram; no success without a
  successful API response and message ID.
- Security uses the bot selected by Railway's SECURITY_BOT_TOKEN and
  SECURITY_BOT_ID; the user authorized replacing this bot.

A masked local dialog collected the replacement token into a mode-0600 file
outside Git. Telegram getMe verified `Gccmonitorformebot`, bot ID `8790108110`.
Both security variables were changed together; the route key, personal chat
destination, training configuration, and service source code were preserved.
Deployment `a0ad72bc-6617-4f85-90fd-ab1d92caa15c` succeeded.

Verification:

- Gmail test message `1a1db00d2e373fbe` returned SENT and INBOX labels.
- Security readiness: HTTP 200, `Gccmonitorformebot`.
- Training readiness: HTTP 200, `Training_vitaliy_bot`.
- Security test via Railway: HTTP 200, `ok=true`, `channel=security`,
  `message_id=196`; key `security-new-bot-8790108110-2026-11-14-setup`.
- Temporary token and task backup files were deleted after verification.

These checks confirm delivery through the new bot and Gmail. A full unattended
scheduled security run has not yet been observed after this change. The
previous bot's internal Telegram failure cause remains unknown.
