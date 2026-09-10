# Second WHOOP participant

The owner requested connecting a second participant to the existing shared-bedroom study. Each person has a separate local profile and separately verified public OAuth and app session. The existing public automation already enumerates all WHOOP connections.

Extend the hourly detail collector with an optional private participant manifest. Preserve the existing session/root as the first target; additional targets declare profile UUID, session file and independent state root. Reject duplicate identities/paths, validate session identity before collection, and isolate failures so one expired session cannot stop another participant. Keep the existing hourly launchd job.

Run existing quality calculations once per explicitly enrolled profile using the same physical sensor stream. Check each person's own public connection, detail state and latest native HR timestamps. Aggregate all participant results into the existing status.json; preserve per-profile daily reports. A healthy first participant must not hide an absent or stale second participant. A sensor is shared, never copied into another person's physiological records.

Keep native resolution, study calendar, raw versions, token renewal and sampling unchanged. Credentials, names, participant manifest and health data stay outside Git. Verify identity separation, partial failure continuation, and per-person freshness with synthetic tests, followed by actual authenticated collection and counts.
