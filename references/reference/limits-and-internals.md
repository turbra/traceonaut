---
slug: /reference/limits-and-internals
title: Limits and Internals
description: Exact parsing, storage and integration boundaries for advanced use.
---

# Limits and Internals

## Codex

### Recorded Command Source

Commands require `event_msg` / `item_completed` with item type `CommandExecution`, matching session ownership, turn/item identity and a valid completion timestamp. Turn and item identities are hashed before persistence.

Completed status with exit code zero is success; failed status with a nonzero exit is failure. Other combinations are unknown. Duration is `secs + nanos / 1e9` when valid, independently of outcome.

Exact duplicates preserve totals; conflicting records make coverage unavailable. Counts use completion time. The complete-history boundary is exclusive and advances past omitted timestamp ties. A covered interval starts after it and needs completed backfill, fresh collection and no unresolved gaps/conflicts.

### Recorded Compaction Source

Compactions require `ContextCompaction` completions with owned identities and valid timestamps. They use separate cursors and bounds, with the same duplicate/conflict rules. They describe an observed event; cause and recovered context capacity are unavailable.

### Scan and Storage Bounds

Each scan reads at most 256 MiB, with 16 MiB per file and an 8 MiB line limit. Oversized lines increment diagnostics. Invalid response-record candidates can invalidate numeric usage for the session. Appended partial lines are retried.

The source inventory is capped at 100,000 files. The private database has a 2 GiB admission check before each scan; leave room for sidecars and one scan's growth. Capacity failures surface through collector health and preserve the existing source and index.

Session retention uses last observed activity, then creation time. Unknown activity remains eligible for the cap. The cutoff is inclusive; session ID breaks equal-activity ties.

### CWO Workflow Audit Inputs

Audit records must be newline-terminated JSON objects with an event type, event hash, and valid source timestamp. Duplicate JSON keys and non-finite values are rejected. Content hashes are verified and used to deduplicate records. Unknown valid event types are exported as `other`.

Each scan reads at most 256 files and 32 MiB, visits 8,192 directory entries, and descends 16 levels under each configured directory. Lines above 256 KiB, malformed records, and incomplete final lines are skipped; incomplete lines can be retried on the next scan. Configured source paths must be owned regular files/directories without symlink traversal.

### CWO Session Association

Association comes from whole user-message skill blocks naming `complex-work-orchestration`, terminal `CommandExecution` records for supported direct Python helper calls under its scripts directory, and native parent/subagent metadata. Plain mentions, documentation reads, and internal Codex tasks are excluded. Arbitrary shell programs and dynamically constructed helper paths are not classified. The supported helper names are listed in [Metrics](metrics.md#cwo-associated-sessions).

Helper outcome and duration are recorded only when the helper is the sole command. A supported helper followed by a receipt read or simple Python post-processing is still associated, but its outcome is unknown and duration is omitted. Help requests, source reads, shell expansion, pipelines, and conditional prefixes are excluded.

Each pass reads at most 64 MiB, up to 32 MiB per file, from at most 4,096 selected rollout files. Candidate records over 8 MiB create a visible source gap. The private feature index stores opaque IDs and cursors. Helper detail is separately capped at 2,000 records within 30 days.

### CWO CLI Review Artifacts

Explicit review bundles require a matching hash-valid `dispatch_prepared` audit event recorded before launch and an explicit timezone-qualified start time. Completed bundles include a final Claude CLI result. Provenance bundles match the prompt hash and dispatch headers against the audit. Launch discovery instead reads recorded Claude or Codex commands and their saved output. Recognized attempts without final output remain visible with unavailable usage. Conflicting copies are omitted; repeated scans and copies preserve one result. See the [custom review adapter](../integrations/custom-review-adapter.md) for artifact fields.

The review reader exports at most 256 invocations. Explicit artifact directories have a 32 MiB scan budget and a 2 MiB file limit. Optional launch discovery shares a separate 32 MiB output budget, with an 8 MiB file limit and 4,096 retained launch records. Review attribution reads at most 64 MiB per pass, 32 MiB per rollout, and 4,096 rollout files. Limits and unsupported recognized launches produce visible gaps. Source files must be owned regular files; symlinks are rejected.

Discovery reads recorded commands without executing them. Checked-command wrappers need a command specification and script saved before the launch, matching prompt and output paths, a matching audit, and consistent timestamps. These saved files identify the source session. The private index retains identifiers, usage, times and review decisions, excluding prompts and review text. Names and command text are not metric labels.

The private review projection history holds at most 4,096 records for 30 days. A row withdrawn before expiry leaves a removal marker; ordinary expiry leaves its final historical sample intact. Reaching the history cap raises the review limit signal until the omitted records expire.

### Account Windows

The weekly strip selects exactly one shared 10,080-minute window. Remaining allowance is `max(0, 100 - usedPercent)`. Reset credits use `availableCount`, because the returned detail list can be capped. Multiple ambiguous account series are suppressed.

## IBM Bob

### Scan and storage bounds

Each changed database is read in one consistent SQLite transaction. Chat records
are refreshed, and message row IDs and hashed identities are compared with the
previous read before fetching new message bodies. A missing or replaced identity
triggers a full message read in that transaction. Skipped messages participate
in the identity check. Invalid identities force a full read.

A full read also runs at startup and after reconnecting. It is scheduled every
five minutes to reconcile older message edits. The schedule uses elapsed time;
incremental reads do not reset it, and an in-progress check finishes first.
Published values are replaced only after
the complete check succeeds. See [Bob data freshness](retention-and-limits.md#ibm-bob)
for the effect on dashboard values.

Bob reads at most 2,000 rows or 4 MiB of JSON per scan slice, with a 0.5-second read
budget and a 100 ms SQLite lock timeout. The continuous collector resumes pending
slices promptly; it waits for the normal polling interval after completion or
failure. A source transaction is limited to 60 seconds; the index is limited to
100,000 tasks, 1,000,000 messages and 512 MiB. A read must fit within all of these limits.
Individual message JSON is limited to 1 MiB; task costs to 64 KiB. Limits and
malformed records are visible in [collection health](collector-health.md#ibm-bob).

The read-only SQLite connection may use Bob's write-ahead log (`-wal`), shared-memory
(`-shm`) and rollback-journal (`-journal`) files. SQLite may update reader markers
in an existing shared-memory file while coordinating with Bob's writer. Database
records and write-ahead log contents remain unchanged.

### Token capture

The optional reader shares Bob's private `bob.sqlite3` index. Each scan reads up to
2,000 events, 4 MiB and 0.5 seconds, across at most 64 journal files. Lines are
limited to 1 MiB. Partial trailing records remain pending. A persisted file cursor
shares the scan budget across rotated files and survives collector restarts.

The supplied file exporter keeps eight 16 MiB backups plus the active file.
Evicting an unread backup reports loss when its unread extent was known.

Only the projected hashed conversation ID, trace/span IDs, timestamps, producer
version and numeric usage enter capture state. Qualified usage requires valid
input/output integers within the safe integer range; a supplied total must equal
their sum. The [token scope](retention-and-limits.md#token-capture) lists counted
event kinds.

Trace/span identity and a signature of the projected fields deduplicate events.
An exact replay preserves totals; a conflict keeps the first event and reports
one loss. File offsets, signatures, totals, scan cursor and the running event-row
count commit in one transaction. A restart reconciles that count once.

The 100,000 stored event-row cap is a rolling window. Rows are pruned when their
first-seen time leaves `--session-retention-seconds`, or their event time precedes
the verified oldest retained journal event. Pruning preserves accumulated tokens.
Compact signatures remain while a retained source references them. Source-floor
advancement waits for complete file inventory and verifies inode, size and change
time. Older timestamps stay outside that floor on later replay. Zero retention
disables time expiry; source-window pruning continues.

At the row cap or near the shared 512 MiB index limit, new capture records are
rejected with visible loss. Row pruning frees the rolling admission count.
Unresolved chat joins remain withheld and report loss when they expire.

Restarts preserve the capture start time and totals. Losing capture state while
its private `bob-otel-epoch.json` marker remains starts a new epoch at EOF and
reports loss. Later events at or before that epoch's start are rejected. Keep the
index and marker together during upgrades and rollback.

## Credential and Bind Validation

A token contains 16–4,096 printable ASCII bytes after trimming, with no embedded whitespace. Its file must be regular, owned by the collector user and mode `0600`. Ancestors must be owned by that user or root; root-owned sticky directories are the writable-directory exception.

The session listener accepts a specific numeric unicast address. Wildcard, multicast and limited-broadcast binds are rejected. Dispatch listeners remain loopback-only.

## CWO Embedding and Recovery

`open_observability_host(Path(config_file))` composes the observer, ledger and metrics service. The controller persists a trusted submission receipt before acknowledgement binding. Completion messages wait in bounded queues for a unique receipt binding.

After restart, the controller supplies trusted receipts through `reconcile_controller_receipts`. Active elapsed time requires proven monotonic-clock continuity. Close the observer outside worker control, drain queues, then close the ledger. `close(timeout_seconds=...) == False` means cleanup is still running.

At capacity, ingestion stops with a visible gap. Retained rows, keys, locks and sidecars count toward admission. Preserve the ledger key and registration generation for recovery.

An embedded `PublicationConfirmer` confirms final Prometheus samples by checking exact stored revisions using the configured job and instance.

See the [host interface](../../scripts/traceonaut/observability_host.py) and [runtime observer](../../scripts/traceonaut/observability_runtime.py) when embedding.
