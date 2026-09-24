---
slug: /reference/data-sources-and-privacy
title: Data Sources and Privacy
description: See which local records the collector reads and which data stays private.
---

# Data Sources and Privacy

The session collector reads `sessions/`, `archived_sessions/`, and selected metadata from `state_*.sqlite` in one Codex profile. It follows new records while Codex runs.

| Source | Collected values |
| --- | --- |
| `session_meta` and the local index | Session identity, project, explicit names and agent relationships. |
| `token_usage_record` | Response identity and token usage. |
| Legacy `token_count` and index counters | Separate runtime-reported token snapshots. |
| Turn events | State, completed/failed turns and supplied durations. |
| `item_completed` / `CommandExecution` | Completion time, exit outcome and reported duration. |
| `item_completed` / `ContextCompaction` | Compaction completion time. |

Repeated response identities are deduplicated. Copied parent history stays with its owning session. Conflicting records make usage unavailable. [Reading the Values](../dashboards/reading-values.md) explains totals and time ranges.

## Privacy

Prometheus receives numeric values and opaque IDs. Explicit session names, agent names/paths and project folder names stay in the protected snapshot and rendered dashboards. Missing names get a project/kind/time fallback.

Prompts, prompt-derived titles, messages, command text, tool output and reasoning are excluded. The collector reads source files without modifying them. Protect the snapshot, database, rendered dashboards and Grafana access.

Each additional profile needs its own collection. [Account Allowance](../optional/account-allowance.md) and [CWO Integration](../integrations/cwo.md) use separate optional sources.
