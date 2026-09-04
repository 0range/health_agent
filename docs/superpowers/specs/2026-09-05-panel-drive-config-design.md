# Management Panel Google Drive Configuration Design

## Goal

Allow a local user to configure or replace the Google Drive source folder for a selected health profile without using Terminal.

## Design

The existing loopback-only profile page renders a small Google Drive form. It accepts one canonical Drive folder URL or opaque folder ID and posts to `/profiles/{profile_id}/drive`. The request uses the panel's existing host, same-origin, CSRF, form-size, and strict route protections.

`PanelService` owns the use case: it first proves that the database profile exists, validates the folder through `DriveProfile.create`, and writes it through `LocalProfileStore` while holding the existing profile sync lock. Replacing roots preserves an already verified Google account binding. A real root change clears the Drive sync cursor; an identical update does not.

The Drive status card becomes a real profile-scoped reader. It displays only safe configuration facts: configured root IDs, whether authorization is still needed, and safe sync status. OAuth credentials never enter the panel view model or HTML.

The POST response re-renders the selected profile with a Russian success or validation message. Unknown profiles remain 404. Malformed, oversized, cross-origin, or invalid-CSRF requests fail with bounded safe messages and no traceback.

## Testing

- HTTP tests cover successful update, canonical URL normalization, invalid/hostile input, oversize input, CSRF and same-origin rejection, and unknown profiles.
- Service tests cover multi-profile isolation, account-binding preservation, cursor reset only after a changed root, and safe Drive cards.
- No test uses live OAuth, live Drive, or the production database.
