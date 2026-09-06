# Medical date recovery

TL;DR: preview a bounded, profile-scoped recovery first, then repeat with `--apply`
after reviewing the aggregate counts:

```console
health-agent review recover-dates --profile-id PROFILE_UUID
health-agent review recover-dates --profile-id PROFILE_UUID --apply
```

The command reads only explicitly labelled collection and issue/report dates from
individual stored document pages. It never prints document text, filenames, or dates,
and apply mode fills only database fields that are still null.

Study, readiness, execution, receipt, birth, registration, and order dates are
intentionally excluded because they do not establish specimen collection or report
issuance. Missing dates stay empty when a label is absent, a value is invalid or in
the future, same-role values conflict, chronology is reversed, or the document already
has a medical-date conflict. Existing dates, review decisions, processing state, and
safe error codes are not changed.
