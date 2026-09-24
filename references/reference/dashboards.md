---
slug: /reference/dashboards
title: Dashboard Reference
description: Dashboard titles, stable UIDs, templates and renderer entry points.
---

# Dashboard Reference

| Title | UID | Template in `examples/observability/` | Renderer in `scripts/` |
| --- | --- | --- | --- |
| [Work Overview](../dashboards/work-overview.md) | `cwo-codex-beta` | `codex-work-overview-beta.json` | `render_codex_beta_dashboard.py` |
| [All Sessions](../dashboards/all-sessions.md) | `cwo-supervisor-observability-v1` | `codex-all-sessions.json` | `render_codex_sessions_dashboard.py` |
| [CWO Overview](../dashboards/cwo.md) | `cwo-dispatch-observability-v1` | `cwo-overview.json` | `render_observability_dashboard.py` |

Dashboard UIDs and renderer script names remain stable for compatibility. Work Overview and All Sessions refresh every 30 seconds; CWO Overview refreshes every minute.

Session renderers read the protected session snapshot; the CWO renderer reads a session snapshot, a presentation registry, or both. Without `--datasource-uid`, output retains Grafana's import-time datasource picker. File provisioning needs an explicit datasource UID.

Each renderer supports `--watch-seconds` from 1 to 60 for name updates. See [Scripts](scripts.md) for arguments and [Automatic Name Updates](../operations/automatic-name-updates.md) for use.
