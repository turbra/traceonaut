---
slug: /reference/dashboards
title: Dashboard Reference
description: Dashboard titles, stable UIDs, templates and renderer entry points.
---

# Dashboard Reference

Dashboard UIDs and renderer names are stable. Session dashboards refresh every
30 seconds; CWO Overview refreshes every minute. All renderers support
`--watch-seconds` from 1 to 60 for automatic name updates.

## Codex

### Identity

| Title | UID | Template in `examples/observability/` | Renderer in `scripts/` | Release bundle |
| --- | --- | --- | --- | --- |
| [Work Overview](../dashboards/work-overview.md) | `cwo-codex-beta` | `codex-work-overview-beta.json` | `render_codex_beta_dashboard.py` | `beta` |
| [All Sessions](../dashboards/all-sessions.md) | `cwo-supervisor-observability-v1` | `codex-all-sessions.json` | `render_codex_sessions_dashboard.py` | `stable` |
| [CWO Overview](../dashboards/cwo.md) | `cwo-dispatch-observability-v1` | `cwo-overview.json` | `render_observability_dashboard.py` | `dispatch` |
| [Codex TUI · Beta](../dashboards/tui-beta.md) | `traceonaut-codex-tui-beta` | `codex-tui-beta.json` | `render_codex_sessions_dashboard.py` | `tui-beta` |

### Names

Session dashboards and CWO Overview use the protected Codex session snapshot.
The CWO renderer also accepts a presentation registry for custom controller
templates. Codex TUI · Beta is an additional experimental session view.

## IBM Bob

### Identity

| Title | UID | Template in `examples/observability/` | Renderer in `scripts/` | Release bundle |
| --- | --- | --- | --- | --- |
| [IBM Bob · Beta](../dashboards/ibm-bob-beta.md) | `traceonaut-ibm-bob-beta` | `ibm-bob-beta.json` | `render_bob_dashboard.py` | `bob-beta` |

### Names

The Bob renderer uses the protected Bob snapshot for project and chat names.

## Rendering

Without `--datasource-uid`, output keeps Grafana's import picker. File provisioning
requires a datasource UID. See [script arguments](scripts.md#dashboard-renderers)
and [automatic name updates](../operations/automatic-name-updates.md).
