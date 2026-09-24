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
