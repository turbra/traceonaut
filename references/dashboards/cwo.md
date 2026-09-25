---
slug: /dashboards/cwo
title: CWO Overview
description: View CWO sessions, agents, usage and workflow activity.
---

# CWO Overview

![CWO Overview with synthetic example data](../../assets/screenshots/cwo-dispatches.png)

*Synthetic data in the current dashboard layout. Counts depend on enabled sources and the selected time range.*

CWO Overview combines observed jobs, workflow audit events, optional CLI review results and CWO-associated Codex sessions. Enable the sources you use through [CWO Integration](../integrations/cwo.md).

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

The main overview shows CWO-associated sessions, native agents, recorded session tokens and helper commands. Choose **Last 1 hour** to see sessions with activity and helper invocations in that hour. The session table appears directly below the headline values.

**Recorded session tokens** covers the selected sessions' whole recorded history, including work outside CWO. It is separate from tokens consumed during the selected hour. Missing values display a dash; known empty counts display zero.

The top strip reports session collection and workflow-source health. **Partial** means the configured sources have incomplete coverage. The default refresh is one minute.

### Sessions and Helper Commands

This section covers the whole configured Codex profile. Association comes from a CWO skill block, a supported helper invocation, or an associated parent session. The table shows session names, projects, models, states and last activity.

**CWO helper commands** and **CWO helper commands by tool** count supported invocations in completed Codex command records within the selected interval, including failed attempts. Compound commands retain an unknown helper outcome because the shell result also covers subsequent commands.

### Workflow Activity

**Workflow events by type** counts events whose original timestamps fall inside the selected range, across configured audit logs. It can be empty while session and helper activity is present: only operations that write audit events appear in that chart.

**CLI review results · by launch time** shows collected Claude CLI reviews launched in the selected range. It includes failed attempts, requested and reported models, input/output tokens and reported duration. Enable the [paired-artifact reader](../integrations/cwo.md#collect-cli-review-results) for this table.

### Optional Observed Jobs

Expand **Observed jobs · optional observation hook** for jobs recorded by the observation hook or [Observed Job Runner](../integrations/observed-job-runner.md). Its completion, failure and token metrics describe those recorded jobs. Ordinary CWO sessions remain in the main overview.

**Observed project** and **Observed task** filter only the observed-job panels and their technical details. They leave the profile-wide overview and workflow sources unchanged.

Job values use the last stored sample per dispatch in the selected range. Completed jobs stop being exported after Prometheus confirms their final samples, so widen the range to find older jobs. **Last response** is the latest recorded model response time, rather than a job finish time.

The collapsed **Technical details** row contains requested/configured models, token provenance and recorded limits.

[Retention and Limits](../reference/retention-and-limits.md) explains source history and exports. [Reading the Values](reading-values.md) explains token totals, partial coverage and missing values.
