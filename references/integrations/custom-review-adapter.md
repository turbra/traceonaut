---
slug: /integrations/custom-review-adapter
title: Custom Review Adapter
description: Collect saved contractor review results, models, effort and token usage.
---

# Custom Review Adapter

CWO Overview shows external Claude and Codex reviews, including models, effort, token usage and review decisions. Enable discovery for reviews launched from CWO-associated Codex sessions, or configure directories containing saved review results.

## Discover Reviews from Sessions

Add these options to your session collector command:

```text
--cwo-sessions --cwo-review-discovery
```

The collector follows saved Claude and Codex CLI launch commands. Results can be in command output or a redirected JSON/JSONL file, including `*.stream.jsonl`. Supported commands include literal account switches, single-command shell scripts, and CWO checked-command wrappers that save the bundles described below.

Discovery follows launches across projects and output directories. Keep temporary output until the collector has read it; collected results remain available after the file is removed. Recognized model checks and CLI login or configuration commands are excluded.

Valid audit events saved beside a review prompt also appear in Workflow activity with their original timestamps.

Check **Review collection** for unsupported commands, unreadable output or missing results. Restore the output or save a supported bundle below; the running collector retries incomplete records.

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

The filename prefix can vary. Each launch record must include `dispatch_id`, `packet_sha256`, a timezone-qualified `started_at`, and `requested_model`. Effort can use `effort`, `requested_effort` or `executed_effort`; populated fields must agree.

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

Numeric Unix timestamps `start_epoch` and `end_epoch` are also accepted as aliases for `started_at` and `finished_at`. When both forms are present, their values must agree.

The saved prompt's SHA-256 must match `prompt_sha256`. Its CWO `Dispatch ID:` and `Packet SHA-256:` headers must match the audit. Traceonaut reads the prompt to verify this match. Bundles without CLI result IDs use the recorded launch to identify the review. Copies count once.

## Check Collection

Run the collector with `--once` and the same collection options. Check `cwo_reviews` for health, pending scans and review counts. With discovery enabled, `cwo_reviews.discovery` lists launch counts and gaps by reason. The running collector finishes pending scans automatically.

CWO Overview's **Review collection** tile shows missing results, read failures and limits. **Saved record** describes result availability for each displayed review. **Execution** shows whether the review command finished or failed; **Evaluation** shows the recorded review decision, including pending peer review.

Select the review's original launch date and set **Project** and **Session** to **All** to include unlinked reviews. Reviews collected later can appear under their original launch date. See [review retention](../reference/retention-and-limits.md#cwo-cli-review-results) for the available history.

See [review metrics](../reference/metrics.md#cli-review-results) for token counts, missing values and model settings.

## Link Reviews to Codex Sessions

Enable `--cwo-sessions` with review discovery or explicit review directories to add source session and attribution. Traceonaut links a review to the Codex session that ran its launch, including a subagent when it was the launcher.

Discovered Claude and Codex launches identify their source session directly. Paired-result `runner.py` launches can also be linked when their saved command output matches the review receipt. Interactive account-switched launches need the recorded stdin command and process identity. See [supported launch evidence](../reference/limits-and-internals.md#cwo-cli-review-artifacts) for wrapper requirements.

| Attribution | Meaning |
| --- | --- |
| Linked | A recorded launch identifies one source session. |
| Unlinked | No supported matching launch was found, or session association is disabled. |
| Pending | Source scanning is incomplete or a relevant source could not be read. |
| Ambiguous | Evidence points to multiple sessions or cannot separate repeated results. |

Bundles without a matching launch remain **Unlinked**. Review usage stays separate from Codex session usage.

For review retention, scan bounds, and source handling, see [Retention and Limits](../reference/retention-and-limits.md#cwo-cli-review-results) and [Limits and Internals](../reference/limits-and-internals.md#cwo-cli-review-artifacts).
