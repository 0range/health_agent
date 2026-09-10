# Room conditions and sleep: prospective personal pilot

Draft collection protocol, 10 September 2026. One consenting account owner now;
a second participant can join later with their own authorization and profile.
The target is 21–28 complete bedroom nights, starting with the first full night
after the device is placed there. Setup readings from the office remain separate.

## Data to preserve

- Room: CO₂, temperature, humidity, PM2.5, PM10, TVOC index and reported noise.
  Preserve original times and values at the finest verified device interval.
- WHOOP: original HR samples (a real test returned approximately six-second
  spacing), sleep stage intervals, scored sleep/recovery records, available stress
  graph data with its actual resolution and provenance. Stress is a WHOOP-derived
  score, not a direct measure of psychological stress. Do not substitute HR for it.
- Original source timestamps in UTC plus timezone, source retrieval time, room
  changes, firmware/settings observations, missing-data periods and revisions.
- Existing morning diary plus notes on nights away, illness, alcohol/caffeine,
  late meals, exercise and unusual noise/window/ventilation changes when known.
  An absent note does not mean the exposure was absent.

## Alignment and quality

Keep native series unchanged. A six-second analysis grid is derived data, not a
claim that independent devices sampled simultaneously. Record matching tolerance,
actual time offsets, rounding rules and missing values. Never fill long gaps or
upsample minute stress scores into apparently measured six-second observations.

Record sample/field coverage and inter-sample gaps for each night's actual WHOOP
sleep interval. Verify clock settings and sensor placement before the first night.
The Qingping API does not yet establish whether noise is instantaneous, averaged,
frequency weighted or an interval maximum; its reported values must not be called
Lmax/Leq or acoustic waveforms. Short sound events can be missed between samples.

## Analysis intent

Use this as an exploratory within-person time-series pilot. Before reviewing
associations, choose a small primary question and specify exposures, outcomes,
lag windows, night inclusion criteria and confounders. Candidate: nightly CO₂
exposure versus sleep efficiency, with temperature and ventilation documented.
Noise-versus-HR changes can be exploratory after timing and noise semantics are
validated. Do not conduct uncontrolled harmful exposure experiments.

Thousands of adjacent samples are correlated observations, not thousands of
independent nights. With 21–28 nights from one/two people, report descriptive
results and uncertainty; no population-wide causal claims or promise of journal
acceptance. A reproducible methods/data-quality report is a realistic first paper
artifact. Freeze the analysis protocol before fitting associations; this draft is
not a preregistration. Publishing any personal data requires a separate decision.

## Interface provenance

[Qingping cloud API](https://developer.qingping.co/cloud-to-cloud/open-apis),
[Qingping device protocol](https://developer.qingping.co/private/communication-protocols/air-monitor/mqtt),
[WHOOP public API](https://developer.whoop.com/api/),
[WHOOP Stress Monitor description](https://support.whoop.com/s/article/Get-to-Know-the-Stress-Monitor?language=en_US).
Detailed WHOOP app interfaces are not the public developer contract and require
separate regression checks and session renewal. Raw exports must identify them
as such; data seen once does not establish unattended collection reliability.

## Start and review schedule agreed 10 September

Archive collection starts today, 10 September 2026, in Europe/Moscow. The setup
day is partial; office readings remain office readings. The daily completeness
review starts 11 September at 10:00 Moscow, backed by hourly freshness/storage
checks and three-day reconciliation. A proposed review cadence is 3–5 complete
bedroom nights for descriptive plots, 7–10 for exploratory hypotheses and 21–28
for a methods/results report. These are work milestones, not validated sample-size
thresholds. The operational six-second coverage threshold is >=99% and no gap over
60 seconds; analysis-specific inclusion rules still need to be fixed before
looking at exposure/outcome associations. See [operations](../runbooks/research-collection.md).
