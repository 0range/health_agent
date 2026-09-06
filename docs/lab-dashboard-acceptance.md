# Laboratory dashboard rendering

Laboratory charts use categorical measurement labels. A date occurring once is shown as
`YYYY-MM-DD`; repeated dates are shown as `YYYY-MM-DD · 1`, `YYYY-MM-DD · 2`, ordered by
document, page, and observation identity. The original date remains in the query output.
This prevents Metabase from aggregating distinct same-day measurements while avoiding
invented collection times. Each chart remains restricted to one registered unit family.

Provisioning discovers only series with verified, dated rows. It can migrate the exact
previous application-owned query, but rejects edited SQL, foreign profiles, databases,
and collections. User cards are preserved. `dashboard setup-labs` and `setup-whoop`
write only local profile-bound origin/dashboard identifiers. The panel reads these local
pointers and never provisions or calls Metabase during a GET.
