---
slug: /integrations/cwo
title: CWO Integration
description: Collect Codex sessions associated with Complex Work Orchestration and read its workflow audit.
---

# CWO Integration

[Complex Work Orchestration (CWO)](https://github.com/gprocunier/complex-work-orchestration) is a Codex skill for coordinating agents and tracking work. Traceonaut reads records already written by Codex and CWO. It does not launch agents.

## Collect CWO Sessions

Add `--cwo-sessions` to your [session collector command](../getting-started.mdx), then restart the collector. This covers the whole configured Codex profile across projects. It associates sessions with supported CWO skill blocks, helper commands, and native subagents of an associated session.

Import [CWO Overview](../dashboards/cwo.md). It shows sessions and agents active in the selected range, their recorded session usage, helper-command counts, and workflow activity when audit collection is enabled. Source gaps and pending scans are shown with the data. [Data Sources and Privacy](../reference/data-sources-and-privacy.md) describes what is read and excluded; [Retention and Limits](../reference/retention-and-limits.md) explains which history is visible.

Session totals include recorded history from before CWO association. Plain mentions of CWO, reading a guide, or generic agent activity do not establish association.

## Collect Workflow Activity

CWO writes its audit log by default to `<CWO installation>/.orchestration-audit/audit.jsonl`. Its `CWO_AUDIT_FILE` setting can select another file. Add the containing directory to the collector with `--cwo-audit-dir`, or pass a custom file directly with `--cwo-audit-file`:

```text
--cwo-audit-dir /absolute/path/to/cwo-installation/.orchestration-audit
```

Append the option to the [session collector command](../getting-started.mdx). Repeat it for multiple CWO installations. For a custom `CWO_AUDIT_FILE`, pass its exact path to `--cwo-audit-file`. Use `--once` with the same options to check source availability, scan coverage, and exported event count.

[CWO Overview](../dashboards/cwo.md) shows recorded workflow activity by event type. Traceonaut exports the newest 2,000 unique events from the last 30 days. Partial reads make displayed counts lower bounds. See [CWO audit retention](../reference/retention-and-limits.md#cwo-workflow-audits).

<a id="collect-external-contractor-reviews"></a>
<a id="link-reviews-to-sessions"></a>

## Read Custom Review Results

Add `--cwo-review-discovery` alongside `--cwo-sessions` to collect external Claude and Codex CLI reviews from recorded launches, including their redirected output files. Use `--cwo-review-dir` for additional saved launch/result pairs or provenance bundles. The [custom review adapter guide](custom-review-adapter.md) covers supported launches, source checks and missing results. Review usage stays separate from Codex session totals.

<a id="existing-controller-metrics"></a>
## Existing Controller Metrics

The advanced controller and dispatch-ledger interface is documented in [Controller Metrics](controller-metrics.md). It is separate from CWO session and workflow collection.

## View and Export

Import [CWO Overview](../dashboards/cwo.md). Existing dispatch ledgers can use [Completed Dispatch Export](terminal-export.md).
