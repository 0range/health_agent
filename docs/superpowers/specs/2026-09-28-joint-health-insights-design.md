# Joint food, weight, recovery and training review

The user wants useful personal health observations from the existing food journal,
dated weight, WHOOP and COROS, instead of separate record counts. They authorize
implementation and continued deployment. Weight currently reaches Apple Health
from Xiaomi Home; a likely S400 scale is being checked separately. WHOOP's body
endpoint is an undated profile snapshot and must never manufacture daily weights.

Use a deterministic evidence layer plus the existing main-agent conversation.
Independent reports would duplicate reminders; free-form model correlation mining
would permit unsupported claims. Keep one existing Sunday 18:00 main-bot message,
add the joint report even without an accepted training plan, and expose `/инсайты`
or `инсайты` on demand. Preserve stopped pushes and stable delivery keys.

Build a bounded 28-day daily dataset using Moscow calendar dates and original
timestamps. Compare the last seven complete days with the preceding seven. Food
amounts are estimates and missing entries are unknown, not fasting. Four logged
slots do not prove a complete diet. Snacks count calories but not full meal slots.
Daily weight medians prevent repeat measurements/copies from overweighting a day;
require at least three dated weight days per week for a weekly difference. Never
interpret scale-weight change as fat loss or combine undated WHOOP body snapshots.

Count unique COROS activities and minutes, preserve day-precision timestamps as
such, and avoid double-counting WHOOP or imported Apple workout copies. For a
no-COROS-record comparison day, require a successful sync covering the full day;
absence still does not prove physical inactivity. Select one longest scored main
sleep per wake date within one WHOOP account; pair scored non-calibrating recovery
by that sleep and connection. Report gaps and sync freshness separately.

Limit exploratory comparisons to two predeclared questions: user-reported dinner-time-to-bed
gap (<2 h vs >=2 h) and a COROS-recorded workout in the preceding 18 hours vs
no recorded COROS workout. Require >=10 paired nights and >=4 per group, exclude
reported/planned away nights, require an explicitly user-reported dinner time for the meal
comparison, and never label observational differences as causal or diagnostic.
Thresholds are engineering display gates, not validated clinical significance.

The short report shows coverage, changes, a supported observation or an explicit
insufficient-data result, and one next step. Persist the exact report and evidence
for replay/audit. Supply the same calculated facts to the main bot for follow-up
questions. Keep source data, identifiers, connection secrets and private output out
of git. Existing clinical handling remains separate.

Reference boundaries: AASM consumer sleep technology position statement
(https://aasm.org/consumer-sleep-technology-position-statement/) cautions against
using consumer wearables as diagnostic substitutes. NIDDK self-monitoring guidance
(https://www.niddk.nih.gov/health-information/weight-management/adult-overweight-obesity/eating-physical-activity)
supports keeping food, activity and weight records; it does not establish causality
for personal correlations or validate this software's display thresholds.
