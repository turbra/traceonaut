<h1 align="center"><img src="assets/traceonaut.png" alt="Traceonaut: Explore every run" width="840"></h1>

<p align="center">
  <strong>Codex session metrics for your existing Prometheus and Grafana.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="assets/license-apache-2.0.svg" alt="License: Apache-2.0"></a>
</p>

<p align="center">
  <a href="https://turbra.github.io/traceonaut/getting-started/">Setup Guide</a> •
  <a href="https://turbra.github.io/traceonaut/dashboards/cwo/">CWO Dashboard</a> •
  <a href="#install">Install</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#documentation">Documentation</a> •
  <a href="#commands-at-a-glance">Commands</a> •
  <a href="#data-scope">Data Scope</a> •
  <a href="#related">Related</a>
</p>

---

Traceonaut reads existing Codex session files and exposes authenticated metrics
for Prometheus. Import its dashboard JSON into Grafana to see recorded token
usage, session activity, command outcomes, and collector health.

```text
Codex files -> collector on your workstation -> Prometheus -> Grafana
```

Prometheus pulls metrics; the collector does not push them. A separate renderer
adds readable names to the dashboard JSON, not metric samples. No Codex telemetry
changes, OpenTelemetry, node_exporter, or alerting services are required.

Observed CWO jobs use a [separate CWO dashboard](references/cwo-dashboard.md).
They are not part of ordinary Codex session metrics.

## Install

Run on the workstation containing your Codex profile. Linux, Bash, and Python
3.13 are the tested environment. Runtime helpers use Python's standard library;
there are no Python packages to install.

```bash
git clone https://github.com/turbra/traceonaut.git
cd traceonaut
python3 scripts/collect_codex_sessions.py --help
```

Use your existing Prometheus and Grafana, either local or on a separate server.
The dashboards use Grafana 11.5 native panels, with no plugins.

## Quick Start

Follow the [three-step setup](references/deployment.md):

1. **[Run the collector](references/deployment.md#1-run-the-collector)** against
   your Codex profile, using a protected scrape credential.
2. **[Configure Prometheus](references/deployment.md#2-add-the-prometheus-scrape-job)**
   to scrape the workstation's `/metrics` endpoint.
3. **[Import a dashboard](references/deployment.md#3-import-a-dashboard-into-grafana)**
   into Grafana and select your Prometheus datasource.

For remote Prometheus, choose a reachable LAN/VPN address with `--host` and allow
that server through the workstation firewall. Loopback is the default. Bearer
authentication does not encrypt HTTP; follow the [network requirements](references/operations.md#network-and-credentials).

No release build, Grafana file provisioning, or extra monitoring service is needed.

## Documentation

- [Online setup and dashboard guides](https://turbra.github.io/traceonaut/): installation, dashboard use, data limits, and operations.
- [Setup](references/deployment.md): workstation and server requirements, collection, scraping, and dashboard import.
- [Operations](references/operations.md): services, automatic name updates, upgrades, and troubleshooting.
- [Session collection](references/codex-all-sessions-observability.md): source formats, retention, and accounting limits.
- [Beta dashboard](references/codex-beta-dashboard.md): session activity, command outcomes, and collector health.
- [Unified dashboard](references/codex-unified-dashboard.md): sessions and agents in one view.
- [CWO dashboard](references/cwo-dashboard.md): optional observed app-server jobs, separate from session views.
- [Account allowance](references/codex-beta-dashboard.md#account-allowance): optional account-wide collection.

## Commands at a Glance

Run scripts with `python3` from the checkout root. Use `--help` for their arguments.

| Script | Purpose |
| --- | --- |
| [Session collector](scripts/collect_codex_sessions.py) | Read local session files and serve Prometheus metrics. |
| [Beta renderer](scripts/render_codex_beta_dashboard.py) | Generate Beta dashboard JSON with readable names. |
| [Unified renderer](scripts/render_codex_unified_dashboard.py) | Generate the combined session and agent dashboard. |
| [Stable renderer](scripts/render_codex_sessions_dashboard.py) | Generate the Stable session dashboard. |
| [Account reader](scripts/collect_codex_account.py) | Optionally read account-wide allowance through Codex. |

### Collector Options

| Option | Purpose |
| --- | --- |
| `--codex-home` | Profile containing `sessions/`, usually `$HOME/.codex`. |
| `--credential-file` | Protected bearer-token file, required when serving metrics. |
| `--host` | Numeric bind address; defaults to `127.0.0.1`. Use a specific LAN/VPN IP for remote scraping. |
| `--port` | Metrics port; defaults to `9464`. |
| `--session-retention-seconds` | Inactivity window; defaults to 30 days. `0` disables age expiry. |
| `--session-export-cap` | Maximum exposed sessions; defaults to `1000`. |
| `--once` | Collect one bounded pass without starting a metrics listener. |

State and snapshot paths are also required; the [setup command](references/deployment.md#1-run-the-collector)
supplies them and keeps output outside the source profile.

## Data Scope

- Session exports default to **30 days of inactivity** and **1,000 sessions**.
  Source files, the private index, and stored Prometheus samples are not deleted.
  Current totals cover exported sessions; choose a past end time to view older
  samples that Prometheus retained.
- Recorded session totals are not interval spending or billing. Account allowance
  is account-wide, not per-session usage. Missing data is not zero.
- Model and effort describe configuration, not proof of the model serving a response.
- Names and selector choices are captured when dashboard JSON is generated.
  Re-render and re-import to add new names, or enable [automatic updates](references/operations.md#automatic-dashboard-name-updates).

Beta is the experimentation dashboard. Unified combines sessions and agents;
Stable is an alternative session view. Existing metric names and dashboard UIDs
retain their `cwo` prefixes for compatibility, not as a dependency on CWO.

## Related

[Complex Work Orchestration (CWO)](https://github.com/gprocunier/complex-work-orchestration)
is a Codex skill for planning complex tasks, coordinating coding agents, and
tracking work across sessions. Traceonaut's [optional integration](references/cwo-integration.md)
exposes metrics for jobs launched by a CWO controller. **You do not need CWO for
ordinary Codex session collection.**

## License

[Apache License 2.0](LICENSE).
