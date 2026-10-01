---
slug: /reference/limits-and-internals
title: Limits and Internals
description: Exact parsing, storage and integration boundaries for advanced use.
---

# Limits and Internals

## Recorded Command Source

Commands require `event_msg` / `item_completed` with item type `CommandExecution`, matching session ownership, turn/item identity and a valid completion timestamp. Turn and item identities are hashed before persistence.

Completed status with exit code zero is success; failed status with a nonzero exit is failure. Other combinations are unknown. Duration is `secs + nanos / 1e9` when valid, independently of outcome.

Exact duplicates preserve totals; conflicting records make coverage unavailable. Counts use completion time. The complete-history boundary is exclusive and advances past omitted timestamp ties. A covered interval starts after it and needs completed backfill, fresh collection and no unresolved gaps/conflicts.

## Recorded Compaction Source

Compactions require `ContextCompaction` completions with owned identities and valid timestamps. They use separate cursors and bounds, with the same duplicate/conflict rules. They describe an observed event; cause and recovered context capacity are unavailable.

## Scan and Storage Bounds

Each scan reads at most 256 MiB, with 16 MiB per file and an 8 MiB line limit. Oversized lines increment diagnostics. Invalid response-record candidates can invalidate numeric usage for the session. Appended partial lines are retried.

The source inventory is capped at 100,000 files. The private database has a 2 GiB admission check before each scan; leave room for sidecars and one scan's growth. Capacity failures surface through collector health and preserve the existing source and index.

Session retention uses last observed activity, then creation time. Unknown activity remains eligible for the cap. The cutoff is inclusive; session ID breaks equal-activity ties.

## IBM Bob

Bob reads at most 2,000 rows or 4 MiB of JSON per scan slice, with a 0.5-second read
budget and a 100 ms SQLite lock timeout. Slices continue over subsequent polls.
A source transaction is limited to 60 seconds; the index is limited to 100,000
tasks, 1,000,000 messages and 512 MiB. Individual message JSON is limited to 1 MiB;
task costs to 64 KiB. Limits and malformed records are visible in collection health.

## CWO Workflow Audit Inputs

Audit records must be newline-terminated JSON objects with an event type, event hash, and valid source timestamp. Duplicate JSON keys and non-finite values are rejected. Content hashes are verified and used to deduplicate records. Unknown valid event types are exported as `other`.

Each scan reads at most 256 files and 32 MiB, visits 8,192 directory entries, and descends 16 levels under each configured directory. Lines above 256 KiB, malformed records, and incomplete final lines are skipped; incomplete lines can be retried on the next scan. Configured source paths must be owned regular files/directories without symlink traversal.

## CWO Session Association

Association comes from whole user-message skill blocks naming `complex-work-orchestration`, terminal `CommandExecution` records for supported direct Python helper calls under its scripts directory, and native parent/subagent metadata. Plain mentions, documentation reads, and internal Codex tasks are excluded. Arbitrary shell programs and dynamically constructed helper paths are not classified. The supported helper names are listed in [Metrics](metrics.md#cwo-associated-sessions).

Helper outcome and duration are recorded only when the helper is the sole command. A supported helper followed by a receipt read or simple Python post-processing is still associated, but its outcome is unknown and duration is omitted. Help requests, source reads, shell expansion, pipelines, and conditional prefixes are excluded.

Each pass reads at most 64 MiB, up to 32 MiB per file, from at most 4,096 selected rollout files. Candidate records over 8 MiB create a visible source gap. The private feature index stores opaque IDs and cursors, not the inspected skill text, arguments, or output. Helper detail is separately capped at 2,000 records within 30 days.

## CWO CLI Review Artifacts

The custom review reader requires a matching hash-valid `dispatch_prepared` audit event recorded before launch, a launch receipt with explicit timezone-qualified start time, and a paired Claude CLI result. File modification time does not replace missing source time. Conflicting copies are omitted; repeated scans and copies preserve one result. See the [custom review adapter](../integrations/custom-review-adapter.md) for the artifact fields.

The review reader scans at most 256 results, 32 MiB per scan, and 2 MiB per file; review attribution has a separate 64 MiB per pass, 32 MiB per file, and 4,096-file bound. Its private index retains at most 4,096 launch evidence records and 4,096 interactive launch records. Source files are read without following symlinks.

## Credential and Bind Validation

A token contains 16–4,096 printable ASCII bytes after trimming, with no embedded whitespace. Its file must be regular, owned by the collector user and mode `0600`. Ancestors must be owned by that user or root; root-owned sticky directories are the writable-directory exception.

The session listener accepts a specific numeric unicast address. Wildcard, multicast and limited-broadcast binds are rejected. Dispatch listeners remain loopback-only.

## Account Windows

The weekly strip selects exactly one shared 10,080-minute window. Remaining allowance is `max(0, 100 - usedPercent)`. Reset credits use `availableCount`, because the returned detail list can be capped. Multiple ambiguous account series are suppressed.

## CWO Embedding and Recovery

`open_observability_host(Path(config_file))` composes the observer, ledger and metrics service. The controller persists a trusted submission receipt before acknowledgement binding. Completion messages wait in bounded queues for a unique binding; timestamps alone cannot establish ownership.

After restart, the controller supplies trusted receipts through `reconcile_controller_receipts`. Active elapsed time requires proven monotonic-clock continuity. Close the observer outside worker control, drain queues, then close the ledger. `close(timeout_seconds=...) == False` means cleanup is still running.

At capacity, ingestion stops with a visible gap. Retained rows, keys, locks and sidecars count toward admission. Preserve the ledger key and registration generation for recovery.

A standalone ledger exporter cannot confirm final samples stored in Prometheus. An embedded `PublicationConfirmer` checks exact stored revisions using the configured job and instance. This confirmation is separate from a successful HTTP scrape.

See the [host interface](../../scripts/traceonaut/observability_host.py) and [runtime observer](../../scripts/traceonaut/observability_runtime.py) when embedding.
