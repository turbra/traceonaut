---
slug: /reference/dashboards
title: Dashboard Reference
description: Dashboard titles, stable UIDs, templates and renderer entry points.
---

# Dashboard Reference

| Title | UID | Template in `examples/observability/` | Renderer in `scripts/` |
| --- | --- | --- | --- |
| [Work Overview](../dashboards/beta.md) | `cwo-codex-beta` | `codex-work-overview-beta.json` | `render_codex_beta_dashboard.py` |
| [Unified](../dashboards/unified.md) | `cwo-codex-unified` | `codex-unified-overview.json` | `render_codex_unified_dashboard.py` |
| [All Sessions](../dashboards/stable.md) | `cwo-supervisor-observability-v1` | `codex-all-sessions.json` | `render_codex_sessions_dashboard.py` |
| [CWO Dispatches](../dashboards/cwo.md) | `cwo-dispatch-observability-v1` | `cwo-observed-dispatches.json` | `render_observability_dashboard.py` |

UIDs and script/template filenames retain their historical identifiers for compatibility. User-facing titles match their guide and navigation labels.

Session renderers read the protected session snapshot; the CWO renderer reads a presentation registry. Without `--datasource-uid`, output retains Grafana's import-time datasource picker. File provisioning needs an explicit datasource UID.

Each renderer supports `--watch-seconds` from 1 to 60 for name updates. See [Scripts](scripts.md) for arguments and [Automatic Name Updates](../operations/automatic-name-updates.md) for use.
