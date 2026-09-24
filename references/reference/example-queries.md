---
slug: /reference/example-queries
title: Example Queries
description: Useful PromQL checks for collection health and recorded session totals.
---

# Example Queries

Run these in Prometheus or Grafana Explore. Choose the datasource scraping Traceonaut.

## Collector Readiness

```promql
up{job="traceonaut"}
cwo_codex_collector_source_available
time() - cwo_codex_collector_scan_timestamp_seconds
cwo_codex_collector_pending_files
```

Expect the first two values to be `1`. Scan age should track the polling interval; pending files should fall as the initial scan progresses.

## Errors and Skips

```promql
sum by (reason) (rate(cwo_codex_collector_errors_total[1h]))
sum by (reason) (cwo_codex_collector_skipped_records_total)
```

See [Collector Health](collector-health.md) for reason meanings and the distinction between errors and expected skips.

## Recorded Tokens in Exported Sessions

```promql
sum(
  cwo_codex_session_usage_tokens{token_kind="total"}
  and on (project_id, session_id)
  (cwo_codex_session_usage_state == 1 or cwo_codex_session_usage_state == 4)
)
```

This sums available and partial recorded history for currently exported sessions. The values are absolute snapshots, so `rate()` and `increase()` are unsuitable for interval spending.

## Sessions by Kind

```promql
count by (kind) (cwo_codex_session_info)
```

Subagents appear as their own sessions. See [Metrics](metrics.md) for names and labels, and [Reading the Values](../dashboards/reading-values.md) for selection semantics.
