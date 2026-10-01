---
slug: /reference/collector-health
title: Collector Health
description: Read scan freshness, error rates and skipped-record reasons.
---

# Collector Health

## Codex

Work Overview and All Sessions have a collector-wide status strip. Project and Work filters leave these values unchanged. Skipped records and detailed coverage are in Diagnostics.

| Signal | Read it as |
| --- | --- |
| Scan age | Seconds since `cwo_codex_collector_scan_timestamp_seconds`. Compare it with your polling interval. |
| Errors | `3600 * rate(cwo_codex_collector_errors_total[1h])`: average errors per hour over the last hour. It needs two samples. |
| Skipped records | Cumulative `cwo_codex_collector_skipped_records_total`, grouped by reason. |
| Source available | `1` means the source can be read. |
| Pending files | Files still to scan. This can be high during initial collection. |

An idle Codex profile can have a healthy collector. **Latest source event age** measures session activity separately from scan age. Missing health samples show **—**.

## Skipped-Record Reasons

| Reasons | Meaning |
| --- | --- |
| `untracked_prefix`, `untracked_item`, `untracked_event` | Expected records outside the collection allowlist. |
| `foreign_session`, `pre_session_history` | Copied/forked history excluded from this session. |
| `unsupported_record_type`, `non_object_record`, `invalid_payload`, `invalid_metadata`, `missing_session`, `invalid_timestamp`, `invalid_identity`, `invalid_usage`, `invalid_record`, `oversized_record` | Unsupported or rejected input. Inspect increases after a Codex upgrade. |

These totals count encounters, so retries and file replays can count a record again. Errors and skips are separate; one record can affect both. Prefix filtering happens before JSON parsing, so this signal cannot detect every format change.

Use the [Example Queries](example-queries.md) for checks in Prometheus. Alerting is left to your monitoring setup.

## IBM Bob

Bob health covers the whole enabled Bob source, independent of dashboard filters.
These signals are gauges showing the latest observed state, not cumulative counters:

| Signal | Read it as |
| --- | --- |
| `traceonaut_bob_collector_source_available` | `1` after a readable source check; `0` before the first check or after a source read failure. |
| `traceonaut_bob_collector_pending` | `1` while records remain to be read in later scans. |
| `traceonaut_bob_collector_collection_complete` | `1` after a full read with no known skipped records or missing values. |
| `traceonaut_bob_collector_last_success_timestamp_seconds` | Time of the latest successful full source check. |
| `traceonaut_bob_collector_source_errors` and `traceonaut_bob_collector_skipped_records` | Latest read failures and skipped records, not rates. |

The [Metrics reference](metrics.md#ibm-bob) lists all Bob health and chat metrics.
