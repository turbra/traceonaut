---
slug: /dashboards/cwo
title: CWO Overview
description: View CWO sessions, agents, usage and workflow activity.
---

# CWO Overview

![CWO Overview with synthetic example data](../../assets/screenshots/cwo-dispatches.png)

*Synthetic data in the current dashboard layout. Counts depend on enabled sources and the selected time range.*

CWO Overview shows Codex sessions and subagents that use CWO: their tokens, model/effort, turn outcomes and recorded turn time. Workflow audits and contractor reviews provide additional detail. Start with `--cwo-sessions` in [CWO Integration](../integrations/cwo.md#collect-cwo-sessions).

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

Import the generated file. Names remain presentation metadata; missing names have explicit fallbacks. See [Automatic Name Updates](../operations/automatic-name-updates.md) for watcher/provisioning use.

## Use the Dashboard

The main overview shows CWO-associated sessions, native agents, recorded session tokens and helper commands. Choose **Last 1 hour** to see sessions with activity and helper invocations in that hour. The session table appears directly below the headline values.

Use **Project** and **Session** to filter the headline counts, session table, helper commands and linked contractor reviews. Both allow multiple selections; **All** clears that filter. A Session selection matches those individual sessions or subagents. Project and Session selections apply together.

The rendered dropdowns use names from the exported session snapshot. Collection health and **Workflow events by type · all projects** remain profile-wide because their records have no project/session identity. Reviews without a proven source session appear only when both dropdowns are **All**.

**Recorded session tokens** covers the selected sessions' whole recorded history, including work outside CWO. It is separate from tokens consumed during the selected hour. Missing values display a dash; known empty counts display zero.

The top strip reports session collection and workflow-source health. **Partial** means the configured sources have incomplete coverage. The default refresh is one minute.

### Sessions and Helper Commands

This section follows the selected projects and sessions. Association comes from a CWO skill block, a supported helper invocation, or an associated parent session.

| Column | Meaning |
| --- | --- |
| Session, Project, Kind | The session or subagent and its project. |
| Model / effort | Latest selected Codex settings. |
| State, Last seen | Latest recorded activity. |
| Tokens | Recorded tokens for the whole session. |
| Turns done, Turns failed | Recorded completed/failed turns across the session. A completed turn can be one step in a larger assignment. |
| Turn time | Sum of reported turn durations. Waiting between turns and other unreported time are excluded. |

The time picker selects sessions active in that range. Their token and turn totals cover recorded session history. A subagent session has its own measurements; a session reused for several assignments combines their measurements.

**CWO helper commands** and **CWO helper commands by tool** count supported invocations in completed Codex command records within the selected interval, including failed attempts. Compound commands retain an unknown helper outcome because the shell result also covers subsequent commands.

### Workflow Activity

**Workflow events by type** counts events whose original timestamps fall inside the selected range, across configured audit logs. It can be empty while session and helper activity is present: only operations that write audit events appear in that chart.

**External contractor reviews** shows who reviewed work for a Codex session: the requested and reported model, requested effort, outcome, token usage and duration. Completed and failed attempts appear when their launch dates fall within the selected range. Enable [contractor review collection](../integrations/cwo.md#collect-external-contractor-reviews) for this table. With [session attribution](../integrations/cwo.md#link-reviews-to-sessions) enabled, **Source session** links to the launching session. Reviews with missing or ambiguous attribution appear when Project and Session are both **All**.

[Retention and Limits](../reference/retention-and-limits.md) explains source history and exports. [Reading the Values](reading-values.md) explains token totals, partial coverage and missing values.
