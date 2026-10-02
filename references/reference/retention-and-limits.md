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

Export retention leaves source records and the Codex index intact. Prometheus keeps already scraped samples according to its own retention policy.

## IBM Bob

Bob uses the same retention settings, with a separate export cap. With both sources
enabled, the default allows up to 1,000 Codex sessions and 1,000 Bob chats, each
active within 30 days.
Chats with no saved messages use their creation time for retention. Source records
are unchanged. Bob's private index mirrors the current database, including deletions.

On each database change, the collector refreshes chat records and token totals,
checks message identities for deletions or replacements, and reads new messages.
Changes to older messages that keep the same identity are picked up by a full check
scheduled every five minutes, or after a collector restart. These changes can
affect response counts, tool results, durations and the last-message time used to
select chats.

Large histories are read over bounded slices. See
[Bob scan and storage bounds](limits-and-internals.md#ibm-bob) for continuation
timing and the limits that can prevent a full read.

The Bob dashboard shows currently exported chats. A chat's recorded totals can
include work from before Prometheus first scraped the collector; charts begin at
that first scrape. The selected time range chooses chats by their last saved
message; chats with no saved messages stay out of the dashboard. Tokens and
responses cover each selected chat's recorded history.
Totals can fall when chats expire from export or the selection changes.

## Historical Views

Current totals include sessions exported at the selected range end. To inspect expired sessions, choose a past end time with stored Prometheus samples. Extending a range that ends now does not restore expired sessions.

Names in the snapshot can outlive exported metrics. Prometheus history begins with the first scrape, even when the collector reads older files.

## Command and Compaction Detail

These detail limits apply to Codex.

Command detail keeps the newest **512 completions within seven days**. Compaction detail separately keeps the newest **64 completions within seven days**.

The dashboard shows the earliest fully covered command/compaction interval. Choose a start after that boundary for complete interval counts. Positive counts can be lower bounds during incomplete collection.

[Limits and Internals](limits-and-internals.md) records scan budgets, tie handling and storage bounds. Export limits bound current metrics, while Prometheus storage grows with retained history.

## CWO Workflow Audits

These optional inputs require Codex collection.

The optional audit input exports the newest **2,000 unique events from the last 30 days**, across all configured files. One event produces one timestamp series. Copies with the same content hash count once. Source files remain unchanged; the collector uses an in-memory projection rather than another database.

Workflow queries use the event's original timestamp, so importing yesterday's log does not count as activity today. Prometheus history still starts with the first scrape: choosing an end time before that scrape cannot show newly imported events. Deleting or moving source logs stops their current export; already scraped samples remain available under Prometheus retention.

See [audit parsing and scan bounds](limits-and-internals.md#cwo-workflow-audit-inputs).

## CWO-Associated Sessions

Association uses the session collector's export window and cap. Older parents can be read to establish a selected child's context. The optional feature has independent cursors and never resets usage accounting.

CWO helper detail retains at most **2,000 command records within 30 days**. Export-cap omissions remain visible until the omitted records age out. Initial backfill can take several scans.

The dashboard selects sessions active in the chosen range that are exported at its end. Token totals cover those sessions' recorded history, including usage before CWO association. Choose a past end time with stored samples to inspect expired sessions.
See [session association parsing and scan bounds](limits-and-internals.md#cwo-session-association).

## CWO CLI Review Results

The optional paired-artifact reader exports at most **256 collected results launched within 30 days**. Source files stay unchanged.

Result identity comes from the CLI session and result UUIDs. Launch times select the interval; file modification times never substitute for missing source timestamps. Repeated scans and copied artifacts preserve one result. Prometheus history begins at the first scrape, including results collected from older files.

Incomplete session scans or unreadable sources can leave attribution Pending while review counts and usage remain visible. Expired or unsupported session sources leave reviews Unlinked; expanding the session export window can make older source histories eligible again.
See [review artifact parsing and scan bounds](limits-and-internals.md#cwo-cli-review-artifacts).
