---
slug: /operations/automatic-name-updates
title: Automatic Name Updates
description: Keep chat names and selectors current with a renderer and Grafana file provisioning.
---

# Automatic Name Updates

Metric values refresh from Prometheus. Names and selector choices come from the snapshot used when the dashboard JSON was rendered.

## Manual Imports

Run the dashboard's renderer again, then re-import its output using the same UID. This is enough for occasional name updates.

## File Provisioning

The renderer watches the collector's private snapshot and writes dashboard JSON.
Grafana reads that JSON through a file provider. Each dashboard needs one watcher
and one provider that owns its UID.

Before switching an imported dashboard, export a backup from Grafana, including
any UI edits. Keep its UID and folder. Provisioning replaces the saved dashboard
with the rendered template; maintain later customizations in that template.

### Prepare a Publication Directory

This Linux example runs the watcher as the collector user and grants Grafana's
service group access to the output directory. Replace `grafana` if your service
uses another group. The snapshot stays in its existing mode-700 directory.

<!-- bob-publication-directory -->
```bash
sudo install -d -o "$USER" -g grafana -m 2750 /var/lib/traceonaut-dashboards/bob
```

The directory's group is inherited by each new file. Dashboard JSON contains chat
names; limit directory access to the watcher and Grafana. The renderer writes
mode-644 files atomically, so Grafana sees a complete old or new version.

For container Grafana, bind-mount **only this output directory**, read-only, at
`/var/lib/traceonaut-dashboards/bob` in the container. Grant its mapped service
user/group read and directory-traversal access; use your container platform's
volume-label option where required. Keep the collector snapshot outside that
mount. For remote Grafana, use a shared filesystem for the output or use manual
imports.

### Run the Bob Watcher

Use the UID of your existing Prometheus datasource from Grafana's datasource
settings. Provisioning uses that UID instead of the import-time picker.
From the Traceonaut checkout:

<!-- bob-name-watcher -->
```bash
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_bob_dashboard.py \
  --template examples/observability/ibm-bob-beta.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --datasource-uid "replace-with-existing-prometheus-uid" \
  --output /var/lib/traceonaut-dashboards/bob/ibm-bob-beta.json \
  --watch-seconds 2
```

Keep this process running, or use the service below. A missing or invalid Bob
snapshot keeps the last rendered dashboard until valid data returns. File
permission, template and output errors stop the watcher and appear in its log.

### Configure Grafana

Save this provider as `traceonaut-bob.yaml` in Grafana's dashboard-provisioning
directory, normally `/etc/grafana/provisioning/dashboards/`. Use the dashboard's
existing folder instead of `Traceonaut` if different. Ensure no other provider
scans the same dashboard UID, including a provider scanning a parent directory.

<!-- bob-name-provider -->
```yaml
apiVersion: 1
providers:
  - name: traceonaut-bob
    orgId: 1
    folder: Traceonaut
    type: file
    disableDeletion: true
    allowUiUpdates: false
    updateIntervalSeconds: 15
    options:
      path: /var/lib/traceonaut-dashboards/bob
```

Restart Grafana once to load the provider. It then checks for changed files every
15 seconds. **Reload the dashboard page to load new names and selector choices.**
The dashboard's Refresh button updates metric queries; it does not reload the
dashboard definition. No repeat render command or manual import is needed.

### Run the Watcher as a Service

Stop the foreground watcher. Save this as
`~/.config/systemd/user/traceonaut-bob-dashboard.service`. Replace the checkout
placeholder throughout the unit and the datasource UID in `ExecStart`. Adjust the snapshot
path if your collector writes elsewhere. Run this user service as the snapshot
owner; it does not depend on Codex collection.

<!-- bob-name-service -->
```ini
[Unit]
Description=Traceonaut Bob dashboard names

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/traceonaut
ExecStart=/usr/bin/python3 /absolute/path/to/traceonaut/scripts/render_bob_dashboard.py --template /absolute/path/to/traceonaut/examples/observability/ibm-bob-beta.json --snapshot-file %h/.local/share/traceonaut/bob.json --datasource-uid replace-with-existing-prometheus-uid --output /var/lib/traceonaut-dashboards/bob/ibm-bob-beta.json --watch-seconds 2
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now traceonaut-bob-dashboard.service
journalctl --user -u traceonaut-bob-dashboard.service -n 20
```

See [Run as a Service](run-as-a-service.md) for starting user services at boot.

### Other Dashboards

For Work Overview, use its renderer and a separate output directory/provider:

```bash
sudo install -d -o "$USER" -g grafana -m 2750 /var/lib/traceonaut-dashboards/work-overview
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_codex_beta_dashboard.py \
  --template examples/observability/codex-work-overview-beta.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --datasource-uid "replace-with-existing-prometheus-uid" \
  --output /var/lib/traceonaut-dashboards/work-overview/work-overview.json --watch-seconds 2
```

Point its provider at `/var/lib/traceonaut-dashboards/work-overview`. Have the
renderer write directly there; a one-time copy will not stay current.

Run the watcher with your service manager. Each dashboard has its own [template and renderer](../reference/dashboards.md).

### Return to Manual Imports

1. Stop the watcher and move its rendered JSON out of every directory scanned by
   the provider. Keep the provider configured with `disableDeletion: true`.
2. Allow at least 15 seconds for Grafana to detect the missing file. Reload the
   dashboard and check that it still opens and is editable. Grafana has now kept
   its saved copy and released file ownership.
3. Remove only this provider's configuration and restart Grafana. Import your
   backup using the same UID and folder, then check folder permissions and
   datasource selection. Leave the collector and Prometheus running.

Keep the backup until these checks pass. Removing the provider before it releases
file ownership can delete the saved dashboard, even with `disableDeletion: true`.

See [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards).
