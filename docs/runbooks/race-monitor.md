# Race registration monitoring — 14 November 2026

## Active architecture

The existing ChatGPT task **Регистрация на старты 2027** owns the daily schedule.
ID: `Automation_ef35471305c4819181e796af6d6a2e2a`.
Enabled; Europe/Moscow; first scheduled run 15 November 2026 at 13:00;
daily through 31 December 2027. Its source conversation is
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
