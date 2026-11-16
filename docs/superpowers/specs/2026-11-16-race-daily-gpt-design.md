# Daily GPT review with independent race checks

The owner approved GPT once daily at 13:00 Europe/Moscow and ordinary code for
the remaining monitoring on 16 November 2026. This change implements that
choice in the existing Railway worker and existing ChatGPT scheduled task.

The worker owns fixed 08:00, 13:00 and 18:00 checks, plus the current hourly
historical-window and five-minute exact-opening cadence. A persisted completed
scan's `checked_at` records its start time. If it predates the latest due fixed
slot, run one catch-up check. Fixed, interval and manual triggers coalesce into
one scan. A restart does not replay all missed slots. The 2027 season cutoff
remains in force. `/status` exposes fixed hours and next due check for inspection.

GPT runs daily at 13:00, reads worker evidence and independently reviews official
event pages and channels `runc_run`, `wn_marathon`, `ironstar_official`. It first
allows the worker's scheduled scan to finish, forcing only a missing fresh scan.
GPT continues ambiguous announcement review, new event discovery and the Sunday
13:00 history/forecast summary. Exact announcements may update `/openings`.

Preserve the current card, quiet unchanged checks, separate forwardable opening
alerts, direct registration links, delivery deduplication and owner-only bot.
No new services, credentials, training-coach activation or security-route changes.

Deploy and verify the worker before reducing the GPT schedule. Verify the saved
prompt and exact RRULE independently. Tests cover fixed boundaries, midnight,
UTC conversion, catch-up, restart and interval coalescing.
