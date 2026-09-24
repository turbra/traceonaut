---
slug: /operations/automatic-name-updates
title: Automatic Name Updates
description: Refresh dashboard names manually or through Grafana file provisioning.
---

# Automatic Name Updates

Metric values refresh from Prometheus. Names and selector choices come from the snapshot used when the dashboard JSON was rendered.

## Manual Imports

Run the dashboard's renderer again, then re-import its output using the same UID. This is enough for occasional name updates.

## File Provisioning

For automatic updates, use a renderer watcher with your existing Grafana file provider:

```bash
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
TRACEONAUT_DASHBOARD_DIR="$TRACEONAUT_DATA_DIR/dashboards"
install -d -m 700 "$TRACEONAUT_DASHBOARD_DIR"
python3 scripts/render_codex_beta_dashboard.py \
  --template examples/observability/codex-work-overview-beta.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --datasource-uid "replace-with-existing-prometheus-uid" \
  --output "$TRACEONAUT_DASHBOARD_DIR/beta.json" --watch-seconds 2
```

Point the provider at a protected copy of the output directory that Grafana can read. Keep the private snapshot separate. Use the actual datasource UID because provisioning skips the import-time picker.

Run the watcher with your service manager. Each dashboard has its own [template and renderer](../reference/dashboards.md).

Back up UI edits before switching to provisioning: provisioned files can replace them. Use one provider per dashboard UID. See [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards).
