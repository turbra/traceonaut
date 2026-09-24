---
slug: /dashboards/cwo
title: CWO Overview
description: View CWO sessions, agents, usage and workflow activity.
---

# CWO Overview

![CWO Overview with synthetic example data](../../assets/screenshots/cwo-dispatches.png)

*Synthetic data in the current dashboard layout. Counts depend on enabled sources and the selected time range.*

CWO Overview combines three sources: observed jobs, workflow audit events and CWO-associated Codex sessions. Enable the sources you use through [CWO Integration](../integrations/cwo.md).

## Import

Import [cwo-overview.json](../../examples/observability/cwo-overview.json) and select your Prometheus datasource.

For readable session names, use the existing private session snapshot:

```bash
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
CWO_DASHBOARD_DIR="$HOME/.local/share/traceonaut/dashboards"
install -d -m 700 "$CWO_DASHBOARD_DIR"
python3 scripts/render_observability_dashboard.py \
  --template examples/observability/cwo-overview.json \
  --session-snapshot-file "$TRACEONAUT_DATA_DIR/sessions-snapshot.json" \
  --output "$CWO_DASHBOARD_DIR/cwo-overview.json"
```

For observed-job names, also pass `--presentation-file /absolute/path/to/private/presentation.json` from the controller or observed-job runner. Either input can be used alone.

Import the generated file. Names remain presentation metadata; missing names have explicit fallbacks. See [Automatic Name Updates](../operations/automatic-name-updates.md) for watcher/provisioning use.

## Use the Dashboard

The top strip shows session scan coverage, audit scan coverage, dispatch telemetry health and publication backlog. **Partial** marks incomplete source coverage. Details are in the collapsed **Technical details** row. The default refresh is one minute.

### Observed Dispatches

**Project** and **Task** filter this section. It shows jobs recorded by the observation hook or [Observed Job Runner](../integrations/observed-job-runner.md).

The default range is 30 days. Values use the last stored sample per dispatch in that range. Completed jobs stop being exported after Prometheus confirms their final samples, so widen the range to find older jobs.

**Last response** is the latest recorded model response time. Jobs without responses stay in the table with a dash. This is a response timestamp, not a job finish time. Requested model and effort appear in the table; acknowledged settings and token coverage are in Technical details.

### Workflow Activity

**Workflow events by type** counts events whose original timestamps fall inside the selected range. It covers all configured audit logs, regardless of Project and Task. The chart shows types with recorded events; the audit status in the top strip distinguishes an empty source from incomplete collection.

### CWO-Associated Sessions

This section covers the whole Codex profile, regardless of Project and Task. Choose **Today** or **Last 7 days** to find associated sessions with activity in that interval.

Association comes from a CWO skill block, a supported helper command, or an associated parent. **Recorded session tokens** covers whole-session history, including work outside CWO. **CWO helper commands** counts recorded command completions in the selected interval, including failed attempts.

Session export and audit history have separate [Retention and Limits](../reference/retention-and-limits.md). [Reading the Values](reading-values.md) explains token totals, partial coverage and missing values.
