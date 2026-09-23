---
slug: /install
---

# Install Traceonaut

Install on the **workstation containing your Codex files**. Prometheus and Grafana
can run locally or on a separate server.

## Requirements

- Git, Bash, and Python. Linux with Python 3.13 is the tested collector environment.
- A readable local Codex profile, usually `$HOME/.codex`.
- Existing Prometheus and Grafana, with a Prometheus datasource in Grafana.
  Dashboards use Grafana 11.5 native panels, with no plugins.

The collector uses Python's standard library. No Python packages, Node.js,
OpenTelemetry, or additional monitoring services are needed.

## Get the collector

Run these commands on the workstation:

```bash
git clone https://github.com/turbra/traceonaut.git
cd traceonaut
python3 scripts/collect_codex_sessions.py --help
```

Keep this checkout and run Traceonaut commands from its root. There is no package
installation or release build step. The Quick Start guide stores generated data
outside the checkout and does not modify your Codex files.

## Next: collect and view metrics

Follow the [Quick Start guide](deployment.md) to run the collector, configure
Prometheus to scrape it, and import a dashboard into Grafana.
