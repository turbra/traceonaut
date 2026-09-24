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

Choose a **Project**, **Work**, and time range. Selecting a work title focuses that session; **All work** clears the Work filter. Project and Work are independent filters.

- **Overview** shows activity and session state.
- **Observed session history** shows recorded token, response and turn totals.
- **Commands** shows completions and failures within the selected interval.
- **Collector health** shows freshness, errors and skipped records.
- Expand **Diagnostics** to inspect coverage and retention.

The **All Sessions** link opens the alternative view with the same filters.

See [Reading the Values](reading-values.md), [Collector Health](../reference/collector-health.md), and [Retention and Limits](../reference/retention-and-limits.md) for the shared meanings.

## Account Allowance

The account strip uses the optional [Account Allowance](../optional/account-allowance.md) collector.

## Display Names

For private aliases, add `--labels-file /absolute/path/to/labels.json` to the renderer command. Create an owner-only `0600` JSON file:

```json
{"version": 1, "projects": {"project-key": "Example App"}, "sessions": {"session-key": "Review changes"}}
```

Use IDs from the snapshot; project aliases must be distinct. These aliases affect Work Overview only. See [Automatic Name Updates](../operations/automatic-name-updates.md) for refreshing names.
