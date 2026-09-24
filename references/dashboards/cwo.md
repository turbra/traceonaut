---
slug: /dashboards/cwo
title: CWO Dispatches
description: View observed CWO jobs, token usage, outcomes and declared budgets.
---

# CWO Dispatches

![CWO Dispatches with synthetic example data](../../assets/screenshots/cwo-dispatches.png)

*Example data. No personal jobs are shown.*

This dashboard shows jobs recorded through [CWO Integration](../integrations/cwo.md) or the [Observed Job Runner](../integrations/observed-job-runner.md). Collector health is reported by `cwo_telemetry_component_state`; recorded jobs appear in `cwo_dispatch_state`.

## Import

Import [cwo-observed-dispatches.json](../../examples/observability/cwo-observed-dispatches.json) and select your Prometheus datasource.

For readable names, use the presentation registry produced by the controller or observed-job runner:

```bash
CWO_PRESENTATION_FILE="/absolute/path/to/private/presentation.json"
CWO_DASHBOARD_DIR="$HOME/.local/share/traceonaut/dashboards"
install -d -m 700 "$CWO_DASHBOARD_DIR"
python3 scripts/render_observability_dashboard.py \
  --template examples/observability/cwo-observed-dispatches.json \
  --presentation-file "$CWO_PRESENTATION_FILE" \
  --output "$CWO_DASHBOARD_DIR/cwo-observed-dispatches.json"
```

Import the generated file. Names remain presentation metadata; missing names have explicit fallbacks. See [Automatic Name Updates](../operations/automatic-name-updates.md) for watcher/provisioning use.

## Use the Dashboard

Summary values use the last stored sample per dispatch within the selected range, which defaults to 30 days. They describe observed jobs retained in that range, rather than jobs started inside it.

If a short range is empty, widen it to include the last observed run. Completed jobs stop being exported after their final samples are confirmed in Prometheus. To see new CWO work, launch authorized jobs through the configured integration or Observed Job Runner. Ordinary Codex session collection alone does not record CWO dispatches.

Dispatches, agents and completed responses are separate counts. Requested/acknowledged settings describe configuration. Token totals carry availability/coverage states. Declared allowances and enforced limits are separate fields.

[Reading the Values](reading-values.md) explains common token and missing-value conventions.
