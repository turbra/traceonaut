---
slug: /reference/data-sources-and-privacy
title: Data Sources and Privacy
description: See which local records the collector reads and which data stays private.
---

# Data Sources and Privacy

Enable each source explicitly. Prometheus receives numeric values and opaque
IDs. Readable names remain in private snapshots and rendered dashboards. Protect
those files, private indexes and Grafana access.

## Codex

### Records

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

### Privacy

The reader excludes prompts, prompt-derived titles, messages, command text, tool
output and reasoning. Explicit session names, agent names/paths and project
basenames remain in the protected snapshot. Missing names get a project/kind/time
fallback. Source files remain unchanged. Each profile needs its own collection.

### Optional features

[Account allowance](../optional/account-allowance.md) reads a separate account
source. [CWO integration](../integrations/cwo.md) adds the following sources.

#### CWO Workflow Audit Inputs

With `--cwo-audit-dir` or `--cwo-audit-file`, Traceonaut reads only the selected audit sources. Prometheus receives the event's content hash as an opaque ID, a bounded event type and its source timestamp, plus numeric collection health. Other fields, including model names, paths, review text and packet contents, are excluded. Source files must be owned by the collector's user; symlinked files and directory paths are rejected. Discovery excludes symlinked subdirectories.

#### CWO Session Association

With `--cwo-sessions`, Traceonaut adds supported CWO association signals to the selected Codex profile. It stores opaque session and command IDs, bounded categories, and numeric timestamps and durations. Skill text, command arguments and output are excluded. Association preserves the session accounting. See [CWO session association details](limits-and-internals.md#cwo-session-association).

#### CWO CLI Review Artifacts

The [optional custom review reader](../integrations/custom-review-adapter.md) reads paired launch receipts, Claude CLI results, and their audit file. Metrics contain hashed result identities, requested/reported model names, requested effort, outcome, source launch time, and numeric usage. Linked review rows use existing Codex project and session IDs. Prompts, response text, account names, paths, and contractor session/result UUIDs stay out of metrics.

## IBM Bob

### Records

The reader opens `db/bob.db` read-only and projects chat identities, saved token
counts, relationships, timestamps, response counts and tool outcomes into a
private index. It makes no network requests and leaves database records unchanged.

### Privacy

Message content, nested messages, arguments, environment objects, credentials and
tool output are excluded. Stored chat titles and project basenames appear in the
protected snapshot and rendered dashboard. Titles can contain personal text.
Metrics contain hashed project/chat IDs and bounded categories. Symlinks,
hardlinked files and writable-by-others paths are rejected.

### Optional features

[Token capture](../optional/bob-token-capture.md) reads a sanitized local journal.
The supplied Collector projection retains hashed conversation IDs, trace/span
IDs, timestamps, a closed producer-version value and numeric generation usage.
Resource/workspace/user metadata is removed before journal persistence; events
and links carrying payloads are rejected.

See [Bob export setup](../optional/bob-token-capture.md#2-enable-bob-export)
for receiver privacy and the shared IBM telemetry setting.
