---
slug: /integrations/custom-review-adapter
title: Custom Review Adapter
description: Adapt custom CWO review receipts and Claude CLI results for optional Traceonaut collection.
---

# Custom Review Adapter

Traceonaut reads an optional `--cwo-review-dir` in a specific custom artifact format. The launch receipts, Claude CLI result files, and runner conventions below are not built-in CWO files or behavior. The reader does not launch reviews or infer usage from prose or filenames.

## Collect Paired Results

Pass an absolute directory containing the artifacts to the session collector:

```text
--cwo-review-dir /absolute/path/to/custom-review-state
```

The reader searches that directory and its subdirectories. The expected layout is:

```text
review/
  audit.jsonl
  reviewer-launch-receipt.json
  reviewer-response.raw.json
```

The filename prefix can vary. Each launch receipt must include `dispatch_id`, `packet_sha256`, a timezone-qualified `started_at`, and `requested_model`; `effort` is optional. The same directory's audit log must contain a hash-valid matching `dispatch_prepared` event recorded before launch. The paired result must be a Claude CLI `type: result` object with `uuid`, `session_id`, and boolean `is_error`.

`--once` reports `cwo_reviews` source health, pending results, and exported review counts. The dashboard shows outcome, requested and reported models, requested effort, token usage, and reported duration. Uncached input, cache-creation input, and cache-read input are distinct token kinds; the dashboard input total includes all three. Thinking is a subset of output. Missing usage remains unavailable; a reported zero remains zero. Duplicate copies count once, conflicting copies are skipped, and repeated audit evaluations do not add usage.

## Link Reviews to Codex Sessions

Enable both `--cwo-sessions` and `--cwo-review-dir` to add source session and attribution to reviews. Traceonaut can link a review to the Codex session that ran its launch, including a subagent when it was the launcher.

Supported evidence includes completed direct Claude commands with matching result identities, and paired-result `runner.py` launches whose recorded output matches the receipt's prompt hash, model, effort, and result metadata. Interactive account-switched launches also need a recorded stdin command tied to the same process. Preparing a dispatch, reading a receipt, or mentioning a review does not establish a link. Other wrappers remain unlinked until supported.

| Attribution | Meaning |
| --- | --- |
| Linked | Recorded launch and result evidence identifies one source session. |
| Unlinked | No supported matching launch was found, or session association is disabled. |
| Pending | Source scanning is incomplete or a relevant source could not be read. |
| Ambiguous | Evidence points to multiple sessions or cannot separate repeated results. |

All collected reviews remain visible, including failures and retries. Requested effort comes from the launch receipt; the result reports a model but does not attest to effort. Review usage stays separate from Codex session usage. Source commands are inspected without execution. Names and command text are not metric labels.

## Custom Audit Event Types

Some custom workflows may write these event names to their audit log:

- `prompt_coached`
- `review_cli_started`
- `review_cli_finished`
- `astra_adjudication_received`

These names are custom-adapter context, not a standard CWO event contract. Traceonaut groups them as **Other audit events** in the standard workflow activity view. Their presence records a custom event only; it does not prove that work completed or establish token usage.

For review retention, scan bounds, and source handling, see [Retention and Limits](../reference/retention-and-limits.md#cwo-cli-review-results) and [Limits and Internals](../reference/limits-and-internals.md#cwo-cli-review-artifacts).
