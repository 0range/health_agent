# Health Agent management panel: product polish v0.1

## Goal

Make the existing loopback-only panel useful at a glance for a non-technical
daily user, without turning it into another application or invoking external
systems.

## Chosen approach

Keep the current Python server-rendered, no-JavaScript architecture. Extend its
small safe view model and local status readers, then redesign the two existing
pages with semantic HTML and responsive CSS. A frontend framework would add
deployment and maintenance cost; CSS-only polish would leave the current raw
status language and missing system areas unresolved.

## Profile overview

The home page keeps profile creation and presents each profile as a clear entry
point. A profile page starts with one concise roll-up: either all known areas are
healthy, synchronization has not run yet, or one or more areas need action.

It shows six stable areas in this order:

1. WHOOP;
2. Google Drive;
3. Gmail;
4. Telegram;
5. Напоминания;
6. Локальная база.

Connector-specific states map into three user states: `connected`,
`not_synced`, and `action_required`. The card headline and main copy are in
Russian and state what the user should understand or do. Healthy cards do not
show a command. A successful synchronization always wins over a merely
`configured` technical status: when `last_success_at` exists, the card cannot
say that synchronization has never run.

Visible remediation follows the failure kind rather than the connector alone.
Authorization failures ask the user to connect or reconnect. Rate limiting asks
them to wait for the next attempt and explicitly says that reconnection is not
needed. Operational synchronization failures ask them to retry or inspect the
status, never to reconnect. Safe codes and exact CLI commands remain secondary
technical details.

UUIDs, account identifiers, exact ISO timestamps, safe error codes, and CLI
commands remain available only inside a native collapsed `details` element.
The page never includes secrets, health values, reminder titles/reasons, raw
documents, filenames, message bodies, or provider payloads.

## Local status sources

Existing WHOOP, Drive, Gmail and Telegram readers remain backward-compatible.
A reminders reader calls the existing profile-scoped aggregate status method and
publishes only counts: awaiting confirmation, scheduled, and due. A database
reader performs a bounded local `SELECT 1`; because the profile itself has already
been loaded, it does not expose row counts or medical data.

Reader failure is isolated to its card and produces a human action-required
state. Rendering a page performs no OAuth, connector synchronization, browser
opening, or external write.

## Destinations

An `Открыть` section contains an obvious safe loopback link to Metabase. The
Google Sheet destination is rendered as a disabled placeholder, `Появится после
подключения Google Таблицы`, until a later integration supplies a verified URL.
Only validated HTTP(S) loopback Metabase URLs and validated Google Docs URLs may
become anchors. Links open in the same tab and do not send a referrer.

## Existing management flows

Profile creation and profile-scoped Drive root replacement retain their routes,
CSRF/origin validation, normalization, and error behavior. The Drive editor is
visually secondary and collapsed under `Настройки Google Drive`, while remaining
fully keyboard-accessible and present in server-rendered HTML.

## Accessibility and responsive behavior

Pages use one `h1`, labelled sections, semantic articles, visible keyboard focus,
text plus icons/colour for state, and at least 44px controls. The card grid is
single-column on narrow screens and expands without horizontal scrolling on
desktop. Native `details` preserves keyboard behavior without client code.

## Tests and acceptance

Unit/request tests cover state grouping, profile isolation, reminders/database
readers, safe escaping, link validation, hidden technical details, headings,
labels, routes, and preserved write workflows. Full repository tests, Ruff and
mypy gates must pass subject only to documented pre-existing failures.

Playwright drives a live local fake-data panel, captures desktop and mobile
screenshots under `output/playwright/`, checks for horizontal overflow, opens
technical details, and verifies the Drive form remains usable. It performs no
real OAuth, connector request, sync, or production-data read.

## Non-goals

No schema, migration, CLI, environment-variable, OAuth, sync, medical-data,
dashboard provisioning, reminder transition, Google Sheets connector, or
external write changes belong to this slice.
