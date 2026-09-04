# Gmail medical ingestion

## TL;DR

The connector foundation is implemented and tested without a real mailbox. It can attach multiple Gmail accounts to one health profile, checks only the last seven days on first connection, then resumes from Gmail `historyId`. Likely medical PDF/images are streamed to an injected importer; generic attachments become a private ambiguity record and do not interrupt the user.

One external step remains: enable Gmail API for a Google Desktop OAuth client and authorize each configured account once.

## Behavior

- OAuth requests exactly `https://www.googleapis.com/auth/gmail.readonly`; the connector exposes no send, modify, trash, or delete operation.
- Initial lookback defaults to `newer_than:7d has:attachment` and is configurable from 1–365 days.
- Each health profile may have multiple account slots such as `personal` and `work`. Email binding, token, cursor, messages, attachments, and vault paths are separated by both `profile_id` and account slot.
- Subsequent scans page through `users.history.list`. If Gmail returns `404` for an old cursor, the connector autonomously repeats the configured lookback and installs a fresh cursor.
- Nested MIME trees and both inline `body.data` and external `attachmentId` forms are supported. Gmail base64url data is decoded incrementally into the importer.
- PDF, JPEG, PNG, TIFF, HEIC/HEIF, and WebP are supported. Generic `application/octet-stream` is accepted only when its filename has a supported extension.
- A supported attachment is considered likely medical only when its filename/subject has a conservative medical signal or its exact sender was configured as trusted. Generic supported files are stored as metadata-only ambiguity; non-medical bodies and attachment bytes are not retained.
- Inline images, unsupported formats, and generic nonmedical messages are ignored. No body, attachment bytes, subject, token, or extracted medical text is logged.
- Message deletion events mark the local occurrence removed without making any Gmail change.

## One-time Google setup

1. Enable Gmail API in the Google Cloud project used for this Mac installation.
2. Configure the OAuth consent screen and add each account as a test user while the personal app remains in testing.
3. Create an OAuth client of type **Desktop app**. The same client JSON may be used by the Drive connector when both APIs are enabled, while each connector keeps a separate exact-scope token.
4. Save the client JSON to the ignored `data/secrets/google-oauth-client.json`.

`gmail.readonly` is a restricted Google scope. This personal local installation does not send mailbox data to a project server; publishing the app for arbitrary external users would require revisiting Google's verification requirements.

## Commands

```bash
uv run health-agent gmail configure PROFILE_UUID personal
uv run health-agent gmail auth PROFILE_UUID personal
uv run health-agent gmail sync PROFILE_UUID personal
uv run health-agent gmail status PROFILE_UUID
```

Repeat `configure` and `auth` with another account slot to attach another mailbox. Omitting the account slot from `sync` or `status` processes every configured account independently. Add a trusted sender only when useful:

```bash
uv run health-agent gmail configure PROFILE_UUID personal --trusted-sender lab@example.com
```

## Integration boundary

This branch intentionally adds no database migration. `GmailService` depends on `GmailStateStore` and `AttachmentImporter`; `AttachmentProvenance` carries profile/account, immutable Gmail message/part/attachment IDs, history ID, internal date, MIME type, source link, SHA-256, and size.

The included `VaultAttachmentImporter` proves streaming and isolation. During integration after migration `0004_chart_integrity`, the production adapter should stage each PDF and call the profile-aware medical importer using `source_provider="gmail"`, a stable message/part external ID, the Gmail source link, and `profile_id`. Image attachments should enter the OCR-capable branch when it lands. The database's profile-scoped SHA constraint and `document_source_records` then deduplicate bytes while retaining Gmail provenance.

## Official references

- [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python)
- [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [List Gmail messages](https://developers.google.com/workspace/gmail/api/guides/list-messages)
- [Synchronize Gmail clients](https://developers.google.com/workspace/gmail/api/guides/sync)
- [Message and MIME resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)
- [Attachment resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments)
- [Error and retry guidance](https://developers.google.com/workspace/gmail/api/guides/handle-errors)
