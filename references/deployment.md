---
slug: /getting-started
---

# Set up Traceonaut

Run Traceonaut on the **workstation containing your Codex files**. Your existing
**Prometheus and Grafana can run on a separate server**. Prometheus pulls metrics
from the workstation; the collector does not push them.

## Requirements

- A readable local Codex profile. Linux, Bash, and Python 3.13 are the tested
  collector environment; no Python packages are needed.
- Existing Prometheus and Grafana, with a Prometheus datasource in Grafana.
  Dashboards use Grafana 11.5 native panels, with no plugins.
- For remote scraping, a workstation LAN/VPN address reachable from Prometheus
  and a firewall rule allowing only that server to reach TCP port `9464`.
  A sleeping, disconnected, or unreachable workstation cannot be scraped.

The endpoint uses **HTTP with bearer authentication, not encryption**. Use a
trusted private LAN or an encrypted VPN. On untrusted networks, use a VPN or your
existing HTTPS proxy; do not expose the HTTP listener to the internet. See
[network and credentials](operations.md#network-and-credentials).

Clone this repository **on the workstation** and run commands from its root:

```bash
git clone https://github.com/turbra/traceonaut.git
cd traceonaut
```

## 1. Run the collector

Set the Codex profile to read. Use the directory containing `sessions/`,
usually `$HOME/.codex`, **not the sessions directory itself**. Collector data
stays separate from that profile.

Set `TRACEONAUT_LISTEN_ADDRESS` below to the workstation's **numeric LAN/VPN IP**
for remote Prometheus. Keep `127.0.0.1` only when Prometheus shares the collector's
network namespace. A separate container's loopback is not the workstation's.
Use a specific address assigned to the workstation, not `0.0.0.0` or `::`.

<!-- setup-paths -->
```bash
export TRACEONAUT_SOURCE_HOME="$HOME/.codex"
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
export TRACEONAUT_METRICS_CREDENTIAL="$TRACEONAUT_DATA_DIR/metrics.token"
export TRACEONAUT_LISTEN_ADDRESS="127.0.0.1"
install -d -m 700 "$TRACEONAUT_DATA_DIR"
```

Create the scrape credential **once**. This refuses to overwrite an existing
credential; skip this block when restarting.

<!-- credential-create -->
```bash
python3 - <<'PY'
import os
import secrets

path = os.environ["TRACEONAUT_METRICS_CREDENTIAL"]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="ascii") as stream:
    stream.write(secrets.token_urlsafe(32) + "\n")
PY
```

Start collection and leave it running. Stop it with **Ctrl+C**.

<!-- run-collector -->
```bash
python3 scripts/collect_codex_sessions.py \
  --codex-home "$TRACEONAUT_SOURCE_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --credential-file "$TRACEONAUT_METRICS_CREDENTIAL" \
  --host "$TRACEONAUT_LISTEN_ADDRESS" --port 9464
```

Metrics are served at `/metrics` on that address and port. Omitting `--host`
retains the safe default, `127.0.0.1`.

## 2. Add the Prometheus scrape job

**On the Prometheus server**, supply a protected copy of the same token, owned
by its service user and mode `0600`. Transfer it through an encrypted channel;
do not paste the token into configuration or logs. For a container, mount that
copy read-only where Prometheus can read it. No Codex files or database are needed
on this server.

Add this entry under `scrape_configs` in your existing configuration. Replace
`workstation.example` with the workstation's reachable IP or DNS name, and the
credential path with its absolute path **as seen by Prometheus**. For same-namespace
loopback collection, use `127.0.0.1:9464` instead. Prometheus does not expand `$HOME`.

<!-- prometheus-scrape -->
```yaml
scrape_configs:
  - job_name: cwo-supervisor-observability
    scrape_interval: 5s
    scrape_timeout: 4s
    authorization:
      type: Bearer
      credentials_file: /absolute/path/to/traceonaut/metrics.token
    static_configs:
      - targets: ['workstation.example:9464']
```

Keep your other jobs. Validate with `promtool check config /path/to/prometheus.yml`,
then reload or restart Prometheus as usual. Check that both queries return `1`:

```promql
up{job="cwo-supervisor-observability"}
cwo_codex_collector_source_available
```

Allow the first scan to finish. A live endpoint alone does not prove source access.

## 3. Import a dashboard into Grafana

In a second terminal **on the workstation**, from the checkout root, generate
Beta's import file.
Use the same data directory if you changed it above:

<!-- render-beta -->
```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_codex_beta_dashboard.py \
  --template examples/observability/grafana-codex-beta-dashboard.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --output "$TRACEONAUT_DATA_DIR/beta.json"
```

In Grafana, open **Dashboards → New → Import**, upload `beta.json`, select your
existing Prometheus datasource, and click **Import**. No Grafana file provisioning
or restart is needed. See [Grafana's import instructions](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/import-dashboards/).

Metrics refresh automatically. Names and selector choices are captured when the
JSON is generated: rerun that command and re-import to add new names.
[Automatic name updates](operations.md#automatic-dashboard-name-updates) are optional.
The raw templates can also be imported, but lack collected names and populated
named selectors.

Use a range ending now to see recent work. Session exports
default to **30 days of inactivity** and **1,000 sessions**; source files and
stored Prometheus history are not deleted.

Other views: [Unified](codex-unified-dashboard.md#add-unified) or
[Stable](codex-all-sessions-observability.md#add-the-stable-dashboard).
Observed CWO jobs use a [separate optional dashboard](cwo-integration.md#add-the-cwo-dashboard).
For services, upgrades, or problems, see [Operations](operations.md).
