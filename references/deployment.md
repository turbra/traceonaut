# Set up Traceonaut

You already have **Prometheus and Grafana**, with a Prometheus datasource in
Grafana. Traceonaut adds one collector and dashboard JSON. No plugins or Python
packages are needed. Linux, Bash, and Python 3.13 are the tested environment;
the dashboards use Grafana 11.5 native panels.

Clone this repository and run the commands from its root:

```bash
git clone https://github.com/turbra/traceonaut.git
cd traceonaut
```

## 1. Run the collector

Set the Codex profile to read. Use the directory containing `sessions/`,
usually `$HOME/.codex`, **not the sessions directory itself**. Collector data
stays separate from that profile.

<!-- setup-paths -->
```bash
export TRACEONAUT_SOURCE_HOME="$HOME/.codex"
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
export TRACEONAUT_METRICS_CREDENTIAL="$TRACEONAUT_DATA_DIR/metrics.token"
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
  --credential-file "$TRACEONAUT_METRICS_CREDENTIAL" --port 9464
```

Metrics are served at **http://127.0.0.1:9464/metrics**, using bearer
authentication. Prometheus must share the collector's network namespace.
For a container or remote Prometheus, see [network access](operations.md#network-and-credentials);
a container's loopback is not the host's.

## 2. Add the Prometheus scrape job

Add this entry under `scrape_configs` in your existing Prometheus configuration.
Replace the credential path with the absolute path **as seen by Prometheus**;
it does not expand `$HOME`.

```yaml
scrape_configs:
  - job_name: cwo-supervisor-observability
    scrape_interval: 5s
    scrape_timeout: 4s
    authorization:
      type: Bearer
      credentials_file: /absolute/path/to/traceonaut/metrics.token
    static_configs:
      - targets: ['127.0.0.1:9464']
```

Keep your other jobs. Validate with `promtool check config /path/to/prometheus.yml`,
then reload or restart Prometheus as usual. Check that both queries return `1`:

```promql
up{job="cwo-supervisor-observability"}
cwo_codex_collector_source_available
```

Allow the first scan to finish. A live endpoint alone does not prove source access.

## 3. Import a dashboard into Grafana

In a second terminal, from the checkout root, generate Beta's import file.
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
For services, upgrades, or problems, see [Operations](operations.md).
