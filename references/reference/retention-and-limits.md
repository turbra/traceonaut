---
slug: /reference/retention-and-limits
title: Retention and Limits
description: Choose which sessions remain visible and understand historical views.
---

# Retention and Limits

## Session Export

By default, the endpoint exposes sessions active within **30 days**, up to **1,000 sessions**. The most recently active sessions take priority. Resuming a session makes it eligible again.

For a 90-day window and a 2,000-session cap, append:

```text
--session-retention-seconds 7776000 --session-export-cap 2000
```

Use `--session-retention-seconds 0` to disable age expiry. The cap accepts 1 through 100,000.

Source files and the private index retain their history. Prometheus keeps already scraped samples according to its own retention policy.

## Historical Views

Current totals include sessions exported at the selected range end. To inspect expired sessions, choose a past end time with stored Prometheus samples. Extending a range that ends now does not restore expired sessions.

Names in the snapshot can outlive exported metrics. Prometheus history begins with the first scrape, even when the collector reads older files.

## Command and Compaction Detail

Command detail keeps the newest **512 completions within seven days**. Compaction detail separately keeps the newest **64 completions within seven days**.

The dashboard shows the earliest fully covered command/compaction interval. Choose a start after that boundary for complete interval counts. Positive counts can be lower bounds during incomplete collection.

[Limits and Internals](limits-and-internals.md) records scan budgets, tie handling and storage bounds. Export limits bound current metrics, while Prometheus storage grows with retained history.

## CWO Workflow Audits

The optional audit input exports the newest **2,000 unique events from the last 30 days**, across all configured files. One event produces one timestamp series. Copies with the same content hash count once. Source files remain unchanged; the collector uses an in-memory projection rather than another database.

Each scan reads at most **256 files and 32 MiB**, visits at most **8,192 directory entries** and descends **16 levels** below each configured directory. Lines over **256 KiB**, malformed records and incomplete final lines are skipped. An incomplete line is retried on the next scan. Collection health reports gaps and limits; counts under partial coverage are lower bounds.

Workflow queries use the event's original timestamp, so importing yesterday's log does not count as activity today. Prometheus history still starts with the first scrape: choosing an end time before that scrape cannot show newly imported events. Deleting or moving source logs stops their current export; already scraped samples remain available under Prometheus retention.

Audit timestamps identify recorded events. They do not establish job completion, worker capacity or token usage.

## CWO-Associated Sessions

Association uses the session collector's export window and cap. Older parents can be read to establish a selected child's context. The optional feature has independent cursors and never resets usage accounting.

Each pass reads at most **64 MiB**, up to **32 MiB per file**, from at most **4,096 selected rollout files**. A candidate record over **8 MiB** is a visible source gap. CWO helper detail retains at most **2,000 command records within 30 days**. Export-cap omissions remain visible until the omitted records age out. Initial backfill can take several scans.

The dashboard selects sessions active in the chosen range that are exported at its end. Token totals cover those sessions' recorded history, including usage before CWO association. Choose a past end time with stored samples to inspect expired sessions.

## CWO CLI Review Results

The optional paired-artifact reader exports at most **256 collected results launched within 30 days**. It shares the audit reader's directory-entry and depth bounds, reads at most **32 MiB per scan**, and limits each file to **2 MiB**. It reads owned regular files without following symlinks. Source files stay unchanged.

Result identity comes from the CLI session and result UUIDs. Launch times select the interval; file modification times never substitute for missing source timestamps. Repeated scans and copied artifacts preserve one result. Prometheus history begins at the first scrape, including results collected from older files.
