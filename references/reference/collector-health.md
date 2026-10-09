---
slug: /reference/collector-health
title: Collector Health
description: Read scan freshness, error rates and skipped-record reasons.
---

# Collector Health

Health values describe each enabled source independently of dashboard filters.

## Codex

### Collection

Work Overview and All Sessions have a collector-wide status strip. Project and Work filters leave these values unchanged. Skipped records and detailed coverage are in Diagnostics.

| Signal | Read it as |
| --- | --- |
| Scan age | Seconds since the last finished scan pass, `cwo_codex_collector_scan_timestamp_seconds`. A pass can leave files pending. |
| Errors | `3600 * rate(cwo_codex_collector_errors_total[1h])`: average errors per hour over the last hour. It needs two samples. |
| Skipped records | Cumulative `cwo_codex_collector_skipped_records_total`, grouped by reason. |
| Source available | `1` means the source can be read. |
| Pending files | Files still to scan. This can be high during initial collection. |

An idle Codex profile can have a healthy collector. **Latest source event age** measures session activity separately from scan age. Missing health samples show **—**.

### Skipped-Record Reasons

| Reasons | Meaning |
| --- | --- |
| `untracked_prefix`, `untracked_item`, `untracked_event` | Expected records outside the collection allowlist. |
| `foreign_session`, `pre_session_history` | Copied/forked history excluded from this session. |
| `unsupported_record_type`, `non_object_record`, `invalid_payload`, `invalid_metadata`, `missing_session`, `invalid_timestamp`, `invalid_identity`, `invalid_usage`, `invalid_record`, `oversized_record` | Unsupported or rejected input. Inspect increases after a Codex upgrade. |

These totals count encounters, so retries and file replays can increment them again. One record can increment both errors and skips. Prefix filtering precedes JSON parsing.

Use the [Example Queries](example-queries.md) for checks in Prometheus. Alerting is left to your monitoring setup.

## CWO Reviews

CWO Overview's **Review collection** tile separates two counts:

- **Collection faults:** read, parsing, conflicting-result and limit errors.
- **Saved evidence gaps:** missing final results, missing bundle provenance and unsupported recorded launches, including older failed runs. A failed run can have a saved launch and no final token usage.

Both counts cover the retained profile. **Saved record** in the review table identifies missing results; their token counts remain unavailable. Evidence coverage remains Partial while results are missing.

Preparation files describe a planned command and are counted separately as expected exclusions. For `missing_provenance`, restore the bundle's matching prompt and audit files to collect its result.

Run the collector with `--once` and its usual options to inspect `cwo_reviews.discovery.gaps`, `pending_results` and `skipped_records`. Restore unreadable output or save a [supported review bundle](../integrations/custom-review-adapter.md); the running collector retries incomplete files. Unsupported recorded launches need a supported wrapper or parser support. Collected numeric results survive removal of temporary output.

## IBM Bob

### Collection

Read the Overview fields as follows:

| Field | Meaning |
| --- | --- |
| Collection | **Available**: readable source and a successful check within 90 seconds. Otherwise **Unavailable / stale**. |
| Scan age | Seconds since the last successful collection check; increases during unfinished reads. |
| Chat data coverage | **Complete**: finished read without skips, invalid values, unsafe parent/subtask reconciliation or unknown tool outcomes. **Partial**: one of those problems or an unfinished read. Omitted token fields leave this field Complete. Hidden while collection is unavailable or stale. |

Checks include incremental and unchanged-source reads. Raw health metrics show the
latest observed state. `traceonaut_bob_collector_pending` is 1 during a bounded
read. Failures describe the latest scan; skipped records describe the latest
completed read. See [history and freshness](retention-and-limits.md#ibm-bob),
[metrics](metrics.md#ibm-bob) and the [source check](../sources/ibm-bob.md#check).

### Token capture

Journal access has independent health. `traceonaut_bob_capture_source_available`
records whether the latest journal check succeeded; its last-success timestamp
measures freshness. A failed check or a success at least 90 seconds old displays
**Capture unavailable / stale**. An idle producer can have fresh journal checks.

`traceonaut_bob_capture_backlog_bytes` measures unread bytes;
`traceonaut_bob_capture_pending_joins` counts unresolved chat identities;
`traceonaut_bob_capture_loss_total` counts detected record losses and rejections.
The capture-start timestamp changes when private capture state is reset. See
[token scope](retention-and-limits.md#token-capture) and
[capture setup](../optional/bob-token-capture.md).
