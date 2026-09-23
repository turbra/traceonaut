---
slug: /dashboards/cwo
---

# CWO dashboard

**CWO · Observed dispatches** is a standalone Grafana dashboard with UID
`cwo-dispatch-observability-v1`. It shows jobs launched through a CWO app-server
controller and observed by Traceonaut. It can be imported alongside the Codex
Beta, Unified, and Stable dashboards.

It does not show ordinary Codex sessions, direct agent work, Beads, native
pools, or report-only activity. [Codex Beta](codex-beta-dashboard.md) is a
separate session dashboard.

## Before you import

Prometheus must already scrape CWO dispatch metrics. [Connect a CWO
controller](cwo-integration.md#connect-an-existing-controller) or [expose an
existing dispatch ledger](cwo-integration.md#read-an-existing-ledger) first.
The ordinary session collector does not create dispatch records; it can only
expose an existing ledger. Check for `cwo_telemetry_component_state` in
Prometheus before expecting dashboard data.

## Import the dashboard

Import [`examples/observability/cwo-observed-dispatches.json`](../examples/observability/cwo-observed-dispatches.json)
in Grafana and select the Prometheus datasource that scrapes the dispatch
metrics. No name registry or Grafana plugin is needed for the metric queries.
Without a name registry, readable task and worker names show as unavailable.

## Show readable names (optional)

If the controller or [observed-job runner](cwo-integration.md#optional-observed-job-runner)
maintains a protected presentation registry, render a named dashboard JSON.
From the checkout root or dispatch release directory, replace the private path:

```bash
CWO_PRESENTATION_FILE="/absolute/path/to/private/presentation.json"
CWO_DASHBOARD_DIR="$HOME/.local/share/traceonaut/dashboards"
install -d -m 700 "$CWO_DASHBOARD_DIR"

python3 scripts/render_observability_dashboard.py \
  --template examples/observability/cwo-observed-dispatches.json \
  --presentation-file "$CWO_PRESENTATION_FILE" \
  --output "$CWO_DASHBOARD_DIR/cwo-observed-dispatches.json"
```

Import the rendered file and select the Prometheus datasource. Re-render and
re-import when names change. For an existing Grafana file-provisioning setup,
use an output location Grafana can read and supply `--datasource-uid` and
`--watch-seconds 2`. Keep the rendered file access-controlled because it
contains display names. Names never become metric labels.

## Read the values

The default time range is 30 days. Summary values use the last stored sample
per dispatch in the selected range; they are not counts of jobs started during
that range. A terminal dispatch can disappear from current exporter output
while its stored Prometheus history remains available. Choose a past end time
to inspect older retained samples.

Dispatches, agents, and completed responses are different units. Requested
and acknowledged settings are not proof of the response model. Reported token
totals can be partial or unavailable; cached input and reasoning output are
subsets, not additional usage. Declared allowances do not change enforcement.
The dashboard does not infer billing, raw reasoning, or an ETA.
