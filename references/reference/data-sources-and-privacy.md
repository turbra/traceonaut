---
slug: /reference/data-sources-and-privacy
title: Data Sources and Privacy
description: See which local records the collector reads and which data stays private.
---

# Data Sources and Privacy

Enable Codex, IBM Bob, or both explicitly.

## Codex

The Codex reader uses `sessions/`, `archived_sessions/`, and selected metadata from `state_*.sqlite` in one profile. It follows new records while Codex runs.

| Source | Collected values |
| --- | --- |
| `session_meta` and the local index | Session identity, project, explicit names and agent relationships. |
| `token_usage_record` | Response identity and token usage. |
| Legacy `token_count` and index counters | Separate runtime-reported token snapshots. |
| Turn events | Session state, **Completed turns**, **Failed / aborted turns**, and reported turn durations. |
| `item_completed` / `CommandExecution` | Completion time, exit outcome and reported duration. |
| `item_completed` / `ContextCompaction` | Compaction completion time. |

Repeated response identities are deduplicated. Copied parent history stays with its owning session. Conflicting records make usage unavailable. [Reading the Values](../dashboards/reading-values.md) explains totals and time ranges.

## Privacy

Prometheus receives numeric values and opaque IDs. Explicit session names, agent names/paths and project folder names stay in the protected snapshot and rendered dashboards. Missing names get a project/kind/time fallback.

The Codex reader excludes prompts, prompt-derived titles, messages, command text, tool output and reasoning. The collector reads source files without modifying them. Protect the snapshot, database, rendered dashboards and Grafana access.

Each additional profile needs its own collection. [Account Allowance](../optional/account-allowance.md) and [CWO Integration](../integrations/cwo.md) use separate optional sources.

## IBM Bob

The Bob reader opens `db/bob.db` using a read-only connection. Source database
records remain unchanged.
The reader stores chat token counts, relationships, timestamps, response counts
and tool outcomes in a private index. It retains no message content, nested messages, arguments, environment
objects, credentials or tool output. It makes no network requests.

Metrics use hashed project/chat IDs and bounded categories. Stored chat titles and
project directory basenames appear only in the private snapshot and rendered
dashboard; titles can contain personal text, so protect Grafana access too.
Symlinks, hardlinked files and writable-by-others paths are rejected.

## CWO Workflow Audit Inputs

These optional inputs belong to Codex collection.

With `--cwo-audit-dir` or `--cwo-audit-file`, Traceonaut reads only the selected audit sources. Prometheus receives the event's content hash as an opaque ID, a bounded event type and its source timestamp, plus numeric collection health. Other fields, including model names, paths, review text and packet contents, are excluded. Source files must be owned by the collector's user; symlinked files and directory paths are rejected. Discovery does not follow symlinked subdirectories.

## CWO Session Association

With `--cwo-sessions`, Traceonaut adds supported CWO association signals to the selected Codex profile. It stores opaque session and command IDs, bounded categories, and numeric timestamps and durations. Skill text, command arguments, and output are not retained or exported. Association does not change session accounting or claim that every token or turn was CWO work. See [CWO session association details](limits-and-internals.md#cwo-session-association).

## CWO CLI Review Artifacts

The [optional custom review reader](../integrations/custom-review-adapter.md) reads paired launch receipts, Claude CLI results, and their audit file. Metrics contain hashed result identities, requested/reported model names, requested effort, outcome, source launch time, and numeric usage. Linked review rows use existing Codex project and session IDs. Prompts, response text, account names, paths, and contractor session/result UUIDs stay out of metrics.
