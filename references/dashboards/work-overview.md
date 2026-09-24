---
slug: /dashboards/work-overview
title: Work Overview
description: View session activity, token usage, command outcomes and collector health.
---

# Work Overview

![Work Overview with synthetic example data](../../assets/screenshots/work-overview.png)

*Example data. No personal sessions are shown.*

Work Overview is the recommended starting view. Follow [Quick Start](../getting-started.mdx#3-import-a-dashboard-into-grafana) to import it.

## Use the Dashboard

Choose a **Project**, **Work**, and time range. Selecting a work title focuses that session. Choose **All** in the Work selector to clear it. Project and Work are independent filters.

- The top strip shows collection health, command coverage and optional account allowance.
- Headlines and the working-sessions chart summarize activity.
- **Work and activity** lists sessions, subagent roles, parent names, usage and command outcomes.
- Expand **Usage** for token breakdowns and the ten highest-usage sessions.
- Expand **Diagnostics** for record coverage, skipped records and export retention.

**Partial** command values are lower bounds, displayed as **≥** in the headlines. Recorded commands and failures remain visible while collection catches up or source records have gaps. Table counts remain numeric for sorting.

Dashboard links switch to **All Sessions** with the same filters and time range. The default refresh is 30 seconds.

See [Reading the Values](reading-values.md) for value meanings and [Collector Health](../reference/collector-health.md) for source checks. The allowance values use the optional [Account Allowance](../optional/account-allowance.md) collector.

## Display Names

For private aliases, add `--labels-file /absolute/path/to/labels.json` to the renderer command. Create an owner-only `0600` JSON file:

```json
{"version": 1, "projects": {"project-key": "Example App"}, "sessions": {"session-key": "Review changes"}}
```

Use IDs from the snapshot; project aliases must be distinct. These aliases affect Work Overview only. See [Automatic Name Updates](../operations/automatic-name-updates.md) for refreshing names.
