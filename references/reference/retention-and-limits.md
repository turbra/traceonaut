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

Export retention leaves source records and private collection state intact. Prometheus keeps already scraped samples according to its own retention policy.

With both sources enabled, these settings apply separately to each source.

## Codex

### Export selection

Selection uses last observed session activity, followed by creation time.

### Historical views

Totals include sessions exported at the selected range end. To inspect expired sessions, choose a past end time with stored Prometheus samples.

Names in the snapshot can outlive exported metrics. Prometheus history begins with the first scrape, even when the collector reads older files.

### Command and Compaction Detail

Command detail keeps the newest **512 completions within seven days**. Compaction detail separately keeps the newest **64 completions within seven days**.

The dashboard shows the earliest fully covered command/compaction interval. Choose a start after that boundary for complete interval counts. Positive counts can be lower bounds during incomplete collection.

[Limits and Internals](limits-and-internals.md) records scan budgets, tie handling and storage bounds. Export limits bound current metrics, while Prometheus storage grows with retained history.

### CWO Workflow Audits

The optional audit input exports the newest **2,000 unique events from the last 30 days**, across all configured files. One event produces one timestamp series. Copies with the same content hash count once. Source files remain unchanged; the collector uses an in-memory projection.

Workflow queries use the event's original timestamp. Imported earlier logs appear at their source time. Prometheus history starts with the first scrape. Deleting or moving source logs stops their current export; earlier samples remain under Prometheus retention.

See [audit parsing and scan bounds](limits-and-internals.md#cwo-workflow-audit-inputs).

### CWO-Associated Sessions

Association uses the session collector's export window and cap. Older parents can be read to establish a selected child's context. The optional feature uses independent cursors and preserves usage accounting.

CWO helper detail retains at most **2,000 command records within 30 days**. Export-cap omissions remain visible until the omitted records age out. Initial backfill can take several scans.

The dashboard selects sessions active in the chosen range that are exported at its end. Token totals cover those sessions' recorded history, including usage before CWO association. Choose a past end time with stored samples to inspect expired sessions.
See [session association parsing and scan bounds](limits-and-internals.md#cwo-session-association).

### CWO CLI Review Results

The optional review reader exports at most **256 invocations launched within 30 days**, including attempts with missing results. Source files stay unchanged.

Result identity comes from the CLI session and result UUIDs when retained. Codex CLI invocations also use their original command identity, keeping resumed attempts separate. Provenance bundles without result IDs use their audited dispatch, packet, prompt hash and launch time. Repeated scans and copied artifacts preserve one result.

The review table selects reviews by their original launch time, including reviews collected later within the retained 30-day inventory. Prometheus history begins when collection starts. Previously exported reviews remain available in historical views after expiry or export-cap omission, while Prometheus retains their samples.

Incomplete session scans or unreadable sources can leave attribution Pending while review counts and usage remain visible. Expired or unsupported session sources leave reviews Unlinked; expanding the session export window can make older source histories eligible again.
See [review artifact parsing and scan bounds](limits-and-internals.md#cwo-cli-review-artifacts).

## IBM Bob

### Export selection

Bob uses the shared inactivity window and its own export cap. Defaults allow
1,000 Bob chats active within 30 days. Chats without saved messages use their
creation time. The private index mirrors the current database, including deletions.

### Historical views

The dashboard selects exported chats by Project, Chat and last saved message time
within the chosen range. Chats without saved messages stay out of that selection.
Saved and captured token charts select the same chats. Saved tokens and responses
cover each selected chat's recorded history; captured totals cover its accepted
generation events. Totals change when chats expire or the selection changes.

Charts begin with the first Prometheus scrape. Choose a past range end with stored
samples to inspect chats that have since expired.

On a database change, collection refreshes chat records, checks message identities
and reads new messages. Edits to older messages with unchanged identities appear
after the full check scheduled every five minutes or after a collector restart.
Large histories continue over bounded scans. See
[scan details](limits-and-internals.md#ibm-bob).

### Token capture

**Partial** means capture includes valid Bob 2.0.5 **LLM Generation** spans received
by the local Collector. Summaries, compaction, auxiliary calls and generations
without valid integer input/output counts are excluded. Resume and parent
generations can omit usable counts. Child generations belong to their own chat.

Saved history and captured totals overlap; compare them separately. Cache counts
are input breakdowns and reasoning counts are output detail. Total tokens are
input plus output. **Estimated tokens in range** uses Prometheus `increase()` over
scrape and commit times, with interpolation.

Outages, sampling, producer exit before flush, unread file eviction and unresolved
chat identities can lose usage. Detected record losses appear in
`traceonaut_bob_capture_loss_total`; calls that never arrive remain unobserved.
Capture begins with telemetry opt-in. Earlier missing counts remain unavailable.

See [token capture setup](../optional/bob-token-capture.md) and
[storage, replay and rotation](limits-and-internals.md#token-capture).
