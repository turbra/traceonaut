---
slug: /reference/data-sources-and-privacy
title: Data Sources and Privacy
description: See which local records the collector reads and which data stays private.
---

# Data Sources and Privacy

Enable each source explicitly. By default, Prometheus receives numeric values,
opaque IDs and bounded categories. Optional command history also exports
sanitized descriptions, names, paths and network addresses as labels. Protect
private files, indexes, metric access and Grafana access.

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

By default, the reader excludes prompts, prompt-derived titles, messages, command text, tool
output and reasoning. Explicit session names, agent names/paths and project
basenames remain in the protected snapshot. Missing names get a project/kind/time
fallback. Source files remain unchanged. Each profile needs its own collection.

### Optional features

[Account allowance](../optional/account-allowance.md) reads a separate account
source. Command history and [CWO integration](../integrations/cwo.md) add the
optional sources below.

#### CWO Workflow Audit Inputs

With `--cwo-audit-dir` or `--cwo-audit-file`, Traceonaut reads only the selected audit sources. Prometheus receives the event's content hash as an opaque ID, a bounded event type and its source timestamp, plus numeric collection health. Other fields, including model names, paths, review text and packet contents, are excluded. Source files must be owned by the collector's user; symlinked files and directory paths are rejected. Discovery excludes symlinked subdirectories.

#### CWO Session Association

With `--cwo-sessions`, Traceonaut adds supported CWO association signals to the selected Codex profile. It stores opaque session and command IDs, bounded categories, and numeric timestamps and durations. Skill text, command arguments and output are excluded. Association preserves the session accounting. See [CWO session association details](limits-and-internals.md#cwo-session-association).

#### Command History

`--command-history` (alias `--cwo-command-history`) saves sanitized command descriptions, working directories and supported curl endpoint origins in the private index and snapshot. It also publishes them as Prometheus metadata labels, together with session and project names. Explicit curl connection reports can supply a local socket IP; otherwise the IP remains unavailable. The existing Prometheus datasource supplies these fields to Grafana on normal refresh.

The default scope is CWO-associated sessions. `--command-history-scope all` explicitly includes commands from all Codex sessions selected for export in the configured profile, including ordinary sessions using local vLLM. It does not inspect other applications or capture packets. Other CWO panels keep their association requirement.

Curl upload-audit labels add a recognized HTTP method, payload kind, canonical upload-option names and up to four literal file paths, bounded to 768 characters together. Inline values, stdin contents and file contents are excluded. Files are not opened to determine their contents, size or existence. Dynamic file paths remain unknown. These fields describe the requested upload, not delivery; the recorded outcome belongs to the whole command invocation, not the HTTP response.

Approved file operands, upload file paths, repository paths, branch names and remote names remain visible. Credential options, configuration values, environment assignments, commit messages, unknown arguments and program bodies are omitted. URL credentials, paths, query strings and fragments are omitted from network endpoints. Secrets embedded in a filename or branch name can still be displayed. Enable this only when readers of the metrics endpoint, Prometheus and Grafana may read those paths, names and addresses.

Snapshots remain owner-only. The existing metrics credential protects the exported labels. Restrict Prometheus and Grafana datasource access, including Explore and query access, to the intended audience. Labels follow Prometheus retention, including configured remote storage. Disabling history stops future export; already scraped labels, backups and saved exports retain their data until removed by their own retention policies.

#### CWO CLI Review Artifacts

The [optional custom review reader](../integrations/custom-review-adapter.md) reads saved launch/results and audits. Launch discovery also reads recorded command output and literal redirected paths, including temporary files. It reads matching prompt headers to establish audit linkage. Private state retains numeric projections, source paths, hashes and original provider IDs for reconciliation. Metrics contain hashed review identities, models, effort, execution/evaluation state, source launch time and numeric usage. Linked rows use existing Codex project/session IDs. Prompts, responses, account names, paths and provider UUIDs stay out of metrics.

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
