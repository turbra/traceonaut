---
slug: /integrations/cwo
title: CWO Integration
description: Show CWO-associated sessions, existing workflow audit logs and optional observed-job metrics.
---

# CWO Integration

[Complex Work Orchestration](https://github.com/gprocunier/complex-work-orchestration) is a Codex skill for coordinating agents and tracking work across sessions. Traceonaut can identify associated Codex sessions, read optional workflow audit logs, and expose separately observed jobs.

## Collect CWO Sessions

Append this flag to your [session collector command](../getting-started.mdx), then restart the collector:

```text
--cwo-sessions
```

This covers the **whole configured Codex profile**, across projects. It uses records already written by Codex:

- A structured CWO skill block associates its owning session.
- A supported direct CWO helper command associates its owning session.
- A native subagent created after its parent's CWO association inherits that association.

Import [CWO Dispatches](../dashboards/cwo.md). Its **CWO-associated sessions** section shows sessions and agents active in the selected range, their recorded session usage, and CWO helper command counts. Initial history scanning runs in bounded passes; the dashboard shows pending files and source gaps.

**Association is session context.** Token totals cover whole sessions, including earlier history, rather than CWO-only cost. Plain mentions of CWO in conversation, reading a guide, or generic agent activity do not establish association. [Data Sources and Privacy](../reference/data-sources-and-privacy.md#cwo-session-association) describes the supported signals.

No per-project audit path or new CWO logging step is needed for this section.

## Collect Workflow Activity

Append this option to your [session collector command](../getting-started.mdx):

```text
--cwo-audit-dir /absolute/path/to/project/.orchestration-audit
```

Repeat `--cwo-audit-dir` for each project's audit directory. Traceonaut finds `audit.jsonl` and `*-audit.jsonl` files in that directory and its subdirectories, including new sprint logs created later. For a different filename, use `--cwo-audit-file /absolute/path/to/workflow.jsonl`. Repeat it for additional files.

Choose the directories or files that your CWO workflow writes. CWO's `CWO_AUDIT_FILE` setting can select a custom location. Traceonaut reads existing files; enabling collection leaves your CWO launch and review workflow unchanged.

The collector exposes `cwo_audit_*` metrics on the **same authenticated endpoint** that Prometheus already scrapes. Import [CWO Dispatches](../dashboards/cwo.md) to see its **Workflow activity** section.

| Recorded event | Dashboard meaning |
| --- | --- |
| `packet_built` | A contractor packet was prepared. |
| `dispatch_prepared` | A dispatch was prepared for execution. |
| `return_evaluated` | A returned review was evaluated. Reevaluations count separately. |
| Review CLI and native-pool audit events | Recorded activity grouped by event type. |

These are workflow events, not completed-job or token totals. The existing audit format supplies an event type, timestamp and content hash. Formats without those fields appear as skipped records. Unknown event types are grouped as **Other audit events**.

Traceonaut verifies each record's content hash and deduplicates copies. It exports the newest **2,000 events within 30 days**, using their original timestamps. See [Retention and Limits](../reference/retention-and-limits.md#cwo-workflow-audits) for scan bounds and historical views.

Use `--once` with the same options for a first-run check. Its `cwo_audit` summary reports source availability, coverage and exported event count. A **complete** status means every selected audit file was read successfully within the limits. This measures those files, not all CWO usage. Incomplete reads make displayed counts lower bounds.

## Observed Job Metrics

Jobs launched with Traceonaut's [Observed Job Runner](observed-job-runner.md), or a controller with the optional observation hook, write a separate `observability.sqlite3` ledger. That source supplies job, token and outcome panels. Workflow audit logs do not populate that ledger.

### Use an Existing Ledger

The simplest option is to append `--state-dir /absolute/path/to/private-ledger` to the session collector command. This exposes dispatch metrics on the existing endpoint.

For a separate endpoint, from the checkout root:

```bash
python3 scripts/export_dispatch_observability.py \
  --state-dir /absolute/path/to/private-ledger \
  --credential-file /absolute/path/to/private/dispatch.token \
  --host 127.0.0.1 --port 9465
```

Set `--port 9465` explicitly: the CLI's compatible default is `9464`, also used by the session collector. Enable the commented `traceonaut-dispatches` job in the [scrape example](../../examples/observability/prometheus-scrape.yaml) only for this separate endpoint.

`--once` prints one metrics snapshot and exits. `--capacity-report` prints local ledger and series counts. Both read committed state without starting jobs.

The standalone endpoint is loopback-only. Use the shared session endpoint for the session collector's supported remote-scrape layout.

### Connect a Controller

Build the observation components:

```bash
TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
TRACEONAUT_DISPATCH_RELEASE="$(python3 scripts/build_release.py --component dispatch --output-dir "$TRACEONAUT_RELEASES_DIR")"
export PYTHONPATH="$TRACEONAUT_DISPATCH_RELEASE/scripts${PYTHONPATH:+:$PYTHONPATH}"
```

For a controller that supports the observation hook, pass `--observability-config` with an absolute path to an owned `0600` JSON file:

| Field | Content |
| --- | --- |
| `state_dir` | Absolute private ledger directory, separate from the checkout and Codex profile. |
| `registration` | [Registration schema](../../schemas/supervisor-project-registration-v1.schema.json) fields without `record_type`; matching process UID and `enabled` state. |
| `executor_vocabulary`, `model_vocabulary`, `effort_vocabulary` | Nonempty allowed label-value lists. |
| `source_compatibility_sha256` | Reference to compatibility evidence for the app-server protocol. |
| `metrics` | Numeric loopback `host`, integer `port`, absolute `credential_file`. |
| `prometheus` (optional) | Loopback `url`, exact scrape `job` and `instance`, optional protected `credential_file`. |

Use protected directories and preserve the ledger key. Traceonaut supplies the observer; CWO owns the controller. Existing ordinary sessions do not become dispatches by enabling this hook.

## View and Export

Import [CWO Dispatches](../dashboards/cwo.md), or use [Completed Dispatch Export](terminal-export.md). [Limits and Internals](../reference/limits-and-internals.md#cwo-embedding-and-recovery) covers embedding, recovery and final-sample confirmation.

The [Observed Job Runner](observed-job-runner.md) is a separate option that starts explicitly authorized model work.
