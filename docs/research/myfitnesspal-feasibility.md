# MyFitnessPal food export feasibility — 17 September 2026

The public developer documentation does not establish an available food-diary
write route for this app. [Diary POST](https://myfitnesspalapi.com/docs/diary-post/)
documents exercise, steps and water writes; meal nutrition can be read. API use
also requires [partner authentication](https://myfitnesspalapi.com/docs/partner-authentication/)
and user authorization. Having a personal login does not establish partner access
or permission to post food. No credentials requested and no upload attempted.

[Apple Health synchronization](https://support.myfitnesspal.com/hc/en-us/articles/360032271092-Apple-Health-connection-and-syncing)
sends food from MyFitnessPal to Apple Health, so writing meals to Apple Health
would not provide the requested reverse import.

The supported immediate option is manual [Quick Add](https://support.myfitnesspal.com/hc/en-us/articles/360032621971-What-is-Quick-Add)
using meal calories shown by our bot. Quick Add macros require Premium. This
copies a total, not the ingredient-level history, and edits must be reconciled
manually. The authoritative history therefore stays in Health Agent.

An automatic exporter needs a separately verified supported food-write agreement
or a tested browser workflow. Internal undocumented endpoints found in third-party
projects have not been validated here and are not offered as working integration.
If later implemented: profile-specific consent, four meal mapping, stable source
meal IDs, update-on-correction rather than duplicate inserts, retry receipts and
read-back verification are necessary. Do not upload previous history implicitly.
