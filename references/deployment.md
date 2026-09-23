# Set up Traceonaut

Connect the file-based session collector to your Prometheus and Grafana
installation, then add the Beta dashboard. No other collection service is needed.

## Prerequisites

- Linux and Bash. Python 3.13 is the tested interpreter; no Python packages are
  required. Older Python versions are untested.
- A Traceonaut checkout and a readable local Codex profile containing rollout
  files in `sessions/` or `archived_sessions/`. The collector reads source files
  without changing them. A Codex process need not be running.
- Prometheus, `promtool`, and Grafana already installed. Native Grafana 11.5 panels
  are the dashboard compatibility baseline; no panel plugins are required.
- An owner-controlled directory outside the Codex profile for private state,
  credentials, rendered dashboards, and releases. Do not use symlinked paths.

The first-use instructions assume the collector, Prometheus, renderer, and
Grafana run as the same Unix user and share a network namespace. For containers
or different service users, read [service boundaries](#service-and-container-boundaries)
before starting. Traceonaut does not install or manage those services.

**Export limits:** the default is 30 days without observed session activity and
at most 1,000 exported sessions, newest activity first. Source files and indexed
history remain intact. Current totals exclude expired/cap-omitted sessions.
Selecting a past end time can show previously scraped sessions within Prometheus
retention; increasing a range ending now cannot restore them. See
[session retention](codex-all-sessions-observability.md#limit-session-exposition).

## 1. Choose paths and build releases

Run from the checkout root, in one Bash shell. `$HOME/.codex` is only a default:
set `TRACEONAUT_SOURCE_HOME` to the profile you want to read. These shell variables
are documentation conveniences, not a new runtime configuration API.

```bash
export TRACEONAUT_ROOT="$(pwd -P)"
export TRACEONAUT_SOURCE_HOME="$HOME/.codex"
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
export TRACEONAUT_RELEASES_DIR="$TRACEONAUT_DATA_DIR/releases"
export TRACEONAUT_SESSION_STATE="$TRACEONAUT_DATA_DIR/session-state"
export TRACEONAUT_PRESENTATION_DIR="$TRACEONAUT_DATA_DIR/presentation"
export TRACEONAUT_DASHBOARD_DIR="$TRACEONAUT_PRESENTATION_DIR/dashboards"
export TRACEONAUT_METRICS_CREDENTIAL="$TRACEONAUT_DATA_DIR/metrics.token"
export TRACEONAUT_PROMETHEUS_UID="traceonaut-prometheus"
export TRACEONAUT_PROMETHEUS_URL="http://127.0.0.1:9090"

test -f "$TRACEONAUT_ROOT/scripts/build_release.py"
test -d "$TRACEONAUT_SOURCE_HOME"
umask 077
install -d -m 700 "$TRACEONAUT_DATA_DIR" "$TRACEONAUT_RELEASES_DIR" \
  "$TRACEONAUT_SESSION_STATE" "$TRACEONAUT_PRESENTATION_DIR" "$TRACEONAUT_DASHBOARD_DIR"

export TRACEONAUT_COLLECTOR_RELEASE="$(python3 "$TRACEONAUT_ROOT/scripts/build_release.py" \
  --component sessions --output-dir "$TRACEONAUT_RELEASES_DIR")"
export TRACEONAUT_BETA_RELEASE="$(python3 "$TRACEONAUT_ROOT/scripts/build_release.py" \
  --component beta --output-dir "$TRACEONAUT_RELEASES_DIR")"
test -f "$TRACEONAUT_COLLECTOR_RELEASE/manifest.json"
test -f "$TRACEONAUT_BETA_RELEASE/manifest.json"
```

Keep all paths absolute after shell expansion and state/output outside the source
profile. Build as the owner of the release directory. Runtime processes use the
hash-named bundles, not a checkout that may change during operation. Bundled
scripts are not executable; invoke them with `python3`.

## 2. Check source access and backfill

```bash
python3 "$TRACEONAUT_COLLECTOR_RELEASE/scripts/collect_codex_sessions.py" \
  --codex-home "$TRACEONAUT_SOURCE_HOME" \
  --session-state-dir "$TRACEONAUT_SESSION_STATE" \
  --snapshot-file "$TRACEONAUT_PRESENTATION_DIR/sessions.json" --once
```

The summary reports `sessions`, `pending_files`, and `source_available`.
Require `source_available: 1`. A zero exit code alone does not prove source health.
`pending_files > 0` means a bounded scan has more data to read; the continuous
collector will continue it. `sessions: 0` with available source can be a genuinely
empty profile. Confirm you selected the right profile before interpreting it.

This command does not listen on a port. Do not run it against a state directory
already owned by a running collector: there must be only one writer.

## 3. Create a protected metrics credential

Create it once. This refuses to overwrite an existing file and does not print
the secret or put it in a process argument.

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

The collector requires a regular file owned by its effective user, mode `0600`,
not a symlink. Ancestors must be real directories owned by that user or root and
not group/world-writable, except root-owned sticky directories such as `/tmp`.
After trimming outer whitespace, the token must be 16 to 4,096 printable ASCII
bytes with no spaces or internal newlines. Private state and snapshot directories
must be owner-only `0700`; snapshots are `0600`. Do not loosen these permissions
to make another service read the credential.

## 4. Start collection and check authentication

```bash
python3 "$TRACEONAUT_COLLECTOR_RELEASE/scripts/collect_codex_sessions.py" \
  --codex-home "$TRACEONAUT_SOURCE_HOME" \
  --session-state-dir "$TRACEONAUT_SESSION_STATE" \
  --snapshot-file "$TRACEONAUT_PRESENTATION_DIR/sessions.json" \
  --credential-file "$TRACEONAUT_METRICS_CREDENTIAL" \
  --host 127.0.0.1 --port 9464 --poll-seconds 5 \
  >"$TRACEONAUT_DATA_DIR/collector.log" 2>&1 &
TRACEONAUT_COLLECTOR_PID=$!
```

The endpoint only permits loopback addresses. Check it without displaying the
token or session metrics. This client disables proxies and redirects.

<!-- metrics-check -->
```bash
python3 - <<'PY'
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

token = Path(os.environ["TRACEONAUT_METRICS_CREDENTIAL"]).read_text(encoding="ascii").strip()
request = Request("http://127.0.0.1:9464/metrics", headers={"Authorization": "Bearer " + token})
try:
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=4) as response:
        payload = response.read()
except HTTPError as error:
    raise SystemExit(f"Metrics HTTP {error.code}; see Troubleshoot below") from None
except URLError:
    raise SystemExit("Metrics connection failed; check process and network namespace") from None
if b"cwo_codex_collector_scan_timestamp_seconds" not in payload:
    raise SystemExit("Expected collector metrics are absent")
print("Authenticated metrics: HTTP 200; collector health metrics present")
PY
```

An initial authenticated `503` means no payload is ready yet. Retry after a scan;
the five-second poll interval is not a first-scan completion deadline. A `200`
proves metrics are served, not that the source is healthy or the backlog empty.
Check those signals in Prometheus and Beta below. An unauthenticated request
must return `401`, not session data.

## 5. Configure and verify Prometheus

The [scrape example](../examples/observability/prometheus-scrape.yaml) is JSON-compatible
YAML. Keep its job name and five-second scrape interval. Generate a local fragment
with the credential path, not the secret:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["TRACEONAUT_ROOT"])
config = json.loads((root / "examples/observability/prometheus-scrape.yaml").read_text())
config["scrape_configs"][0]["authorization"]["credentials_file"] = os.environ["TRACEONAUT_METRICS_CREDENTIAL"]
output = Path(os.environ["TRACEONAUT_DATA_DIR"]) / "prometheus-traceonaut.yaml"
output.write_text(json.dumps(config, indent=2) + "\n")
PY
promtool check config "$TRACEONAUT_DATA_DIR/prometheus-traceonaut.yaml"
```

Merge this `scrape_configs` entry into your active Prometheus configuration.
Do not replace other jobs or add a duplicate job with the same name. Validate the
merged file with `promtool check config /absolute/path/to/prometheus.yml`, then
reload or restart through your existing service workflow. Prometheus reads scrape
configuration through `--config.file`, not `--web.config.file`.

In Prometheus **Status → Target health**, require the
`cwo-supervisor-observability` target to be **UP**. In the query page, check:

```promql
up{job="cwo-supervisor-observability"}
cwo_codex_collector_source_available
cwo_codex_collector_pending_files
time() - cwo_codex_collector_scan_timestamp_seconds
```

Expect `up = 1`, `source_available = 1`, and a recent completed scan. Initial
backfill can leave a nonzero backlog. Empty query results are missing data, not
zero. Prometheus history starts with its first scrape; backfill does not create
samples dated before that scrape. Traceonaut does not set Prometheus retention.
See the [Prometheus configuration reference](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
for the scrape and authorization fields.

## 6. Render and provision Beta

First render a single definition using the actual Prometheus datasource UID. Use
the UID above for a new datasource or set it to your existing datasource's UID.

```bash
python3 "$TRACEONAUT_BETA_RELEASE/scripts/render_codex_beta_dashboard.py" \
  --template "$TRACEONAUT_BETA_RELEASE/examples/observability/grafana-codex-beta-dashboard.json" \
  --snapshot-file "$TRACEONAUT_PRESENTATION_DIR/sessions.json" \
  --datasource-uid "$TRACEONAUT_PROMETHEUS_UID" \
  --output "$TRACEONAUT_DASHBOARD_DIR/beta.json"
```

For a new datasource and file provider, generate provisioning files locally:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["TRACEONAUT_DATA_DIR"])
datasource = {"apiVersion": 1, "datasources": [{
    "name": "Traceonaut Prometheus", "uid": os.environ["TRACEONAUT_PROMETHEUS_UID"],
    "type": "prometheus", "access": "proxy", "url": os.environ["TRACEONAUT_PROMETHEUS_URL"],
    "jsonData": {"timeInterval": "5s"}}]}
provider = {"apiVersion": 1, "providers": [{
    "name": "traceonaut", "type": "file", "folder": "Traceonaut",
    "disableDeletion": True, "allowUiUpdates": False, "updateIntervalSeconds": 10,
    "options": {"path": os.environ["TRACEONAUT_DASHBOARD_DIR"]}}]}
(root / "grafana-datasource.yaml").write_text(json.dumps(datasource, indent=2) + "\n")
(root / "grafana-dashboards.yaml").write_text(json.dumps(provider, indent=2) + "\n")
PY
```

Place the datasource file in your Grafana provisioning `datasources/` directory
and the provider file in its `dashboards/` directory, using your existing
configuration-management workflow. Do not overwrite an unrelated provider or
define a second datasource with an existing UID. Set the URL and dashboard path
as Grafana sees them, not as your shell sees them. Reload/restart Grafana through
that workflow. See [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).

Open **Connections → Data sources**, select the configured datasource, and use
**Save & test**. Then open the **Traceonaut** folder and Beta (UID
`cwo-codex-beta`). Use a range ending now that includes your session's activity.
Confirm readable Project/Work selectors, a healthy Collection value, scan age,
and the **Collector health** row. The error-rate panel needs at least two samples.
Skipped-record totals include expected exclusions; they are not an error count.
No alerts are enabled.

Keep selectors current by rerunning the renderer in watch mode:

```bash
python3 "$TRACEONAUT_BETA_RELEASE/scripts/render_codex_beta_dashboard.py" \
  --template "$TRACEONAUT_BETA_RELEASE/examples/observability/grafana-codex-beta-dashboard.json" \
  --snapshot-file "$TRACEONAUT_PRESENTATION_DIR/sessions.json" \
  --datasource-uid "$TRACEONAUT_PROMETHEUS_UID" \
  --output "$TRACEONAUT_DASHBOARD_DIR/beta.json" --watch-seconds 2 \
  >"$TRACEONAUT_DATA_DIR/beta-renderer.log" 2>&1 &
TRACEONAUT_RENDERER_PID=$!
```

These shell processes do not survive logout/reboot reliably. After verification,
stop just these processes and put the same pinned commands into your existing
service manager. Traceonaut does not provide a service installer.

```bash
kill -TERM "$TRACEONAUT_RENDERER_PID" "$TRACEONAUT_COLLECTOR_PID"
wait "$TRACEONAUT_RENDERER_PID" "$TRACEONAUT_COLLECTOR_PID"
```

Do not remove the source or private database to stop collection.

## Service and container boundaries

- `127.0.0.1:9464` must be reachable **from Prometheus's network namespace**.
  A container's loopback is not the host's loopback. Place the collector in the
  same namespace using your deployment's existing mechanism. Do not change its
  bind address to `0.0.0.0` or assume a shared bridge makes host loopback reachable.
- Prometheus must read `credentials_file` inside its filesystem namespace. If it
  runs as another UID, provision a separate protected copy of the same token for
  that service. Keep the collector's original owned by the collector, mode `0600`.
  Rotate both copies together and restart the collector; it reads the token at
  startup. Reload/restart Prometheus as required by your deployment.
- The renderer needs its owner's protected snapshot. Grafana only needs the
  rendered dashboard, not the snapshot or collector database. For another UID,
  use your existing deployment workflow to supply an access-controlled dashboard
  copy after rendering. The renderer's output checks still apply; do not weaken
  snapshot or state permissions. The same-user example avoids this extra step.
- Grafana must reach its configured Prometheus URL from **Grafana's** namespace.
  The datasource UID embedded in the dashboard must match the configured source.
  Keep Grafana authenticated: rendered definitions contain local display names.

## Troubleshoot

| Symptom | Check |
| --- | --- |
| Collector exits before listening | Absolute paths, ownership/modes, no symlinks, source directory exists, and only one writer uses the state directory. Inspect the private collector log. |
| Connection refused | Process exited, port conflict, wrong port, or wrong network namespace. |
| Metrics HTTP 401 | Credential content differs or Authorization header is missing. Check protected copies without printing their contents. |
| Metrics HTTP 503 | No rendered payload yet. Allow the first scan to finish; inspect logs and source/state access if it persists. |
| HTTP 200 but source unavailable | Endpoint health is not source health. Check `source_available`, scan age, backlog, and errors. |
| Prometheus target down | Validate the merged config, token file readability from Prometheus, namespace, and scrape timeout. |
| Grafana has no data | Verify datasource reachability/UID, Prometheus queries, selected end time, filters, and session export limits. |
| Names missing or stale | Verify snapshot freshness, renderer log, output permissions, and Grafana provider path. Snapshot metadata does not supply metric history. |
| Account allowance unavailable | Account collection is optional and separate; see [account setup](codex-beta-dashboard.md#account-allowance). |

## Upgrade and roll back

Available build components are `sessions`, `account`, `stable`, `beta`, `unified`,
and `dispatch`. `sessions` and `account` intentionally produce the same collector
bundle with both entrypoints. Presentation bundles are separate and include their
templates and helper scripts. Runtime bundles include the license and manifest,
not the documentation tree. `manifest.json` records file SHA-256 hashes; the
directory name hashes that manifest. Existing bundles are checked, not modified.

1. Save the current pinned release paths, service commands/private configuration,
   and dashboard output outside Git. Keep their access restrictions. Verify the
   saved release files against their manifest before relying on rollback.
2. Validate the changed component, then build it into the same private releases
   directory. Render dashboard changes once into a separate temporary output and
   verify them before replacing the provisioned definition.
3. Change only the affected service's release path/configuration using your
   existing workflow. Preserve state, snapshot paths, credentials, datasource UID,
   dashboard UID, and provisioning destination. Restart only affected processes.
   If account and session services share one release path, update/restart them
   together. A presentation-only update does not need a collector restart.
4. Repeat the endpoint, scrape, and Grafana checks above. If they fail, restore
   the saved release paths/configuration and prior rendered definition, then
   restart the same affected processes. Keep the private database intact.

Keep custom dashboard changes in the source template and rebuild its release.
Direct Grafana edits can be overwritten by the file provider.
