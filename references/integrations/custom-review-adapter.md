---
slug: /integrations/custom-review-adapter
title: Custom Review Adapter
description: Collect saved contractor review results, models, effort and token usage.
---

# Custom Review Adapter

Traceonaut collects external review invocations, models, CLI effort, token usage and evaluator verdicts in CWO Overview. Enable launch discovery for reviews started inside CWO-associated Codex sessions. Use explicit directories for additional saved review bundles.

## Discover Reviews from Sessions

Add these options to your session collector command:

```text
--cwo-sessions --cwo-review-discovery
```

The collector follows recorded Claude and Codex CLI launches. It reads final results saved in command output or an explicitly redirected JSON/JSONL file, including `*.stream.jsonl`. Literal account-switching commands and single-command shell wrappers are supported. Supported CWO checked-command wrappers can also identify a saved provenance bundle through their exact prompt and output paths.

Discovery follows launch records across projects, so a new output directory needs no collection-directory change. Temporary output must still exist when first read. Collected numeric results remain in the private index after that output is removed. Exact readiness probes and CLI authentication/configuration commands are excluded from reviews.

Hash-valid audit events beside discovered review prompts also feed Workflow activity. Collected events survive removal of the temporary directory and keep their original event IDs and timestamps.

Unsupported launch syntax, unreadable output and missing final results appear as collection gaps. Restore the output or save a supported bundle below; the running collector retries incomplete records. It never executes a recorded command.

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

Run the collector with `--once` and the same collection options. Its `cwo_reviews` output reports source health, pending scans and exported review counts. With launch discovery enabled, `discovery` also reports observed launches, separate model checks and gaps by reason. An initial scan can need several passes; the running collector continues automatically.

CWO Overview's Review collection tile shows evidence gaps. The review table retains attempts without a final result and leaves their usage blank. Execution describes the CLI invocation. Evaluation describes the matching audit verdict, including pending peer review. Neither field establishes that the requested implementation was delivered.

Select a time range containing the review's original launch and set Project and Session to All. Linked reviews also appear under those filters. Historical ranges include evidence collected later, within the retained 30-day review inventory. The latest projection excludes superseded pending records and old attribution states.

Uncached input, cache-creation input, and cache-read input are distinct token kinds; the dashboard input total includes all three. Thinking is a subset of output. Missing usage remains unavailable; a reported zero remains zero. Conflicting copies are skipped, and repeated audit evaluations do not add usage.

## Link Reviews to Codex Sessions

Enable `--cwo-sessions` with review discovery or explicit review directories to add source session and attribution. Traceonaut links a review to the Codex session that ran its launch, including a subagent when it was the launcher.

Supported evidence includes completed direct Claude commands with matching result identities, and paired-result `runner.py` launches whose recorded output matches the receipt's prompt hash, model, effort, and result metadata. Interactive account-switched launches also need a recorded stdin command tied to the same process. Preparing a dispatch, reading a receipt, or mentioning a review does not establish a link. Other wrappers remain unlinked until supported.

| Attribution | Meaning |
| --- | --- |
| Linked | Recorded launch and result evidence identifies one source session. |
| Unlinked | No supported matching launch was found, or session association is disabled. |
| Pending | Source scanning is incomplete or a relevant source could not be read. |
| Ambiguous | Evidence points to multiple sessions or cannot separate repeated results. |

All collected reviews remain visible with both filters set to All, including failures, retries and Unlinked reviews. Provenance bundles without supported launch evidence remain Unlinked. Review usage stays separate from Codex session usage. Source commands are inspected without execution. Names and command text are not metric labels.

For review retention, scan bounds, and source handling, see [Retention and Limits](../reference/retention-and-limits.md#cwo-cli-review-results) and [Limits and Internals](../reference/limits-and-internals.md#cwo-cli-review-artifacts).
