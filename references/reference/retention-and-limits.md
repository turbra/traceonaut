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
