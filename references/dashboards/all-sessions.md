---
slug: /dashboards/all-sessions
title: All Sessions
description: Use a compact session-centered view of recorded usage and activity.
---

# All Sessions

![All Sessions with synthetic example data](../../assets/screenshots/all-sessions.png)

*Example data. No personal sessions are shown.*

All Sessions pairs a session inventory with activity and recorded token totals.

## Import

Start the collector with [Quick Start](../getting-started.mdx). From the checkout root:

<!-- render-stable -->
```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_codex_sessions_dashboard.py \
  --template examples/observability/codex-all-sessions.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --output "$TRACEONAUT_DATA_DIR/all-sessions.json"
```

Import `all-sessions.json` in Grafana and select your Prometheus datasource.

## Use the Dashboard

Select **Project**, **Session** and a time range. Choose **All** in the Session selector to include all matching sessions. Subagents appear as their own sessions.

[Reading the Values](reading-values.md) explains history totals, activity states and missing values. [Automatic Name Updates](../operations/automatic-name-updates.md) explains selector refreshes.

The session table combines the latest selected model and effort in **Model / effort**.
**Turn time** adds up recorded completed-turn durations. Expand a cell to inspect a long name.
The collapsed **Diagnostics** section holds **Usage source** and **Runtime reported** for checking token coverage.

The status strip leads directly into the inventory. **Usage** contains ranked comparisons; the working-sessions chart shows activity over time. The default refresh is 30 seconds.
