---
slug: /integrations/custom-review-adapter
title: Custom Review Adapter
description: Collect saved contractor review results, models, effort and token usage.
---

# Custom Review Adapter

Traceonaut reads saved contractor reviews from each directory supplied with `--cwo-review-dir`. It shows their model, CLI effort, token usage, duration and outcome in CWO Overview. Review scripts must save one of the formats below.

## Collect Paired Results

Pass an absolute directory containing the artifacts to the session collector:

```text
--cwo-review-dir /absolute/path/to/custom-review-state
```

The reader searches the directory and its subdirectories. Repeat the option for each project or shared review directory. Use a parent directory where future reviews will also be saved.

Two paired-result layouts are supported:

```text
review/
  audit.jsonl
  reviewer-launch-receipt.json
  reviewer-response.raw.json

review/
  audit.jsonl
  reviewer-launch.json
  reviewer-response.json
```

The filename prefix can vary. Each launch record must include `dispatch_id`, `packet_sha256`, a timezone-qualified `started_at`, and `requested_model`. Effort can use `effort`, `requested_effort` or `executed_effort`; populated fields must agree. These fields describe the CLI setting, not provider-confirmed effort.

The same directory's `audit.jsonl` or `contract-audit.jsonl` must contain a hash-valid matching `dispatch_prepared` event recorded before launch. The paired result must be a Claude CLI `type: result` object with `uuid`, `session_id`, and boolean `is_error`.

## Collect Provenance Bundles

Review scripts that retain a stream of Claude events can save:

```text
review/
  contract-audit.jsonl
  reviewer-provenance.json
  reviewer-prompt.txt
```

The provenance JSON needs `started_at`, `requested_model`, `prompt_sha256`, and an `events` array containing exactly one final `type: result` event. Effort uses the same fields as paired results. Only the final result contributes usage; repeated assistant events are excluded.

The saved prompt's SHA-256 must match `prompt_sha256`. Its CWO `Dispatch ID:` and `Packet SHA-256:` headers must match the audit. Traceonaut reads the prompt to verify that binding; prompt text is never retained in metrics or collector state. Bundles without CLI result IDs use an identity derived from the recorded launch. Copies count once.

## Check Collection

Run the collector with `--once` and the same review directory options. Its `cwo_reviews` output reports source health, pending results and exported review counts. Compare that count with the saved reviews you expect; health covers only configured directories. An attempt without a saved final result appears as pending, including a timed-out review whose result was lost.

In CWO Overview, select a time range containing the review's launch and set Project and Session to All. Reviews with a verified source-session link also appear under those filters. If newer reviews are missing, check their source directory and file layout first.

Uncached input, cache-creation input, and cache-read input are distinct token kinds; the dashboard input total includes all three. Thinking is a subset of output. Missing usage remains unavailable; a reported zero remains zero. Conflicting copies are skipped, and repeated audit evaluations do not add usage.

## Link Reviews to Codex Sessions

Enable both `--cwo-sessions` and `--cwo-review-dir` to add source session and attribution to reviews. Traceonaut can link a review to the Codex session that ran its launch, including a subagent when it was the launcher.

Supported evidence includes completed direct Claude commands with matching result identities, and paired-result `runner.py` launches whose recorded output matches the receipt's prompt hash, model, effort, and result metadata. Interactive account-switched launches also need a recorded stdin command tied to the same process. Preparing a dispatch, reading a receipt, or mentioning a review does not establish a link. Other wrappers remain unlinked until supported.

| Attribution | Meaning |
| --- | --- |
| Linked | Recorded launch and result evidence identifies one source session. |
| Unlinked | No supported matching launch was found, or session association is disabled. |
| Pending | Source scanning is incomplete or a relevant source could not be read. |
| Ambiguous | Evidence points to multiple sessions or cannot separate repeated results. |

All collected reviews remain visible with both filters set to All, including failures, retries and Unlinked reviews. Provenance bundles without supported launch evidence remain Unlinked. Review usage stays separate from Codex session usage. Source commands are inspected without execution. Names and command text are not metric labels.

For review retention, scan bounds, and source handling, see [Retention and Limits](../reference/retention-and-limits.md#cwo-cli-review-results) and [Limits and Internals](../reference/limits-and-internals.md#cwo-cli-review-artifacts).
