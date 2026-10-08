---
slug: /reference/example-queries
title: Example Queries
description: Useful PromQL checks for collection health and recorded session totals.
---

# Example Queries

Run queries in Prometheus or Grafana Explore using the datasource scraping
Traceonaut. Endpoint readiness is shared:

```promql
up{job="traceonaut"}
```

Expect `1`.

## Codex

### Collector Readiness

```promql
cwo_codex_collector_source_available
time() - cwo_codex_collector_scan_timestamp_seconds
cwo_codex_collector_pending_files
```

Expect source availability to be `1`. Scan age should track the polling interval; pending files should fall as the initial scan progresses.

### Errors and Skips

```promql
sum by (reason) (rate(cwo_codex_collector_errors_total[1h]))
sum by (reason) (cwo_codex_collector_skipped_records_total)
```

See [Collector Health](collector-health.md) for reason meanings and the distinction between errors and expected skips.

### Recorded Tokens in Exported Sessions

```promql
sum(
  cwo_codex_session_usage_tokens{token_kind="total"}
  and on (project_id, session_id)
  (cwo_codex_session_usage_state == 1 or cwo_codex_session_usage_state == 4)
)
```

This sums available and partial recorded history for currently exported sessions. Read these values as absolute snapshots.

### Sessions by Kind

```promql
count by (kind) (cwo_codex_session_info)
```

Subagents appear as their own sessions. See [Metrics](metrics.md) for names and labels, and [Reading the Values](../dashboards/reading-values.md) for selection semantics.

## IBM Bob

### Collector Readiness

```promql
traceonaut_bob_collector_source_available
time() - traceonaut_bob_collector_last_success_timestamp_seconds
traceonaut_bob_collector_pending
```

Expect source availability `1` and recent scan age. Pending becomes `0` after the
bounded initial read completes.

### Errors and Skips

```promql
traceonaut_bob_collector_source_errors
sum by (reason) (traceonaut_bob_collector_skipped_records)
```

These gauges show the latest scan/read state. See [collector health](collector-health.md#ibm-bob).

### Recorded Tokens in Exported Sessions

```promql
sum(
  traceonaut_bob_session_usage_tokens{token_kind="total"}
  and on (project_id, session_id)
  (traceonaut_bob_session_token_status{token_kind="total"} == 0)
)
```

This sums available saved history. See [token capture](../optional/bob-token-capture.md)
for the optional generation counter.

### Sessions by Kind

```promql
count by (kind) (traceonaut_bob_session_info)
```

Bob kinds are `normal`, `subtask` and `subagent`.
