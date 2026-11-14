# Security notification delivery recovery

The user authorized adding email and replacing the security Telegram bot on
14 November 2026 after the existing bot began returning Telegram HTTP 502.

## Delivery design

Keep the existing enabled ChatGPT security task and its 08:00 Moscow schedule.
Preserve its research, alert, weekly-report, and silence conditions. Its current
editor already authorizes Gmail for alerts and weekly summaries; make this
independent delivery requirement explicit. Send the authorized report to the
confirmed account, vitali.arz@gmail.com, even if Telegram is unavailable.
Record each channel's actual result, including Gmail message ID when available.
An email test must identify itself as a delivery test, not a security report.

The alternatives are email only after Telegram failure or email on every
authorized report. Preserve the latter, which is already in the current task,
to avoid weakening its existing delivery policy. Daily no-change emails remain
out of scope; existing silence rules still apply.

## Telegram bot replacement

Accept the new token through a masked local dialog and a temporary file with
mode 0600 outside Git. Verify it with Telegram getMe before any configuration
change. Reject the training bot as a security replacement. Update both
SECURITY_BOT_TOKEN and SECURITY_BOT_ID together in Railway, preserve the
security route key and personal destination, and verify readiness and one
test delivery. The user must start the new bot to authorize private messages.
Delete the temporary token file after transferring it successfully.

Leave training routing and its 13:00 Moscow schedule intact. Do not install a
new local scheduler or duplicate either cloud task.

## Verification

Read back the saved task instructions and schedule. Send one labeled Gmail
test and verify the API receipt. After the new token is supplied, check both
Railway readiness routes and confirm a security test receipt from the new bot.
Never treat a timeout or an unconfirmed receipt as delivered.
