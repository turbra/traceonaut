# Operate Traceonaut

These are optional details for an existing installation.
Start with the [three-step setup](deployment.md).

## Network and credentials

The **session collector** defaults to `127.0.0.1`. For remote scraping, set
`--host` to a specific numeric IPv4 or IPv6 address assigned to the workstation's
LAN/VPN interface. Hostnames, wildcard addresses (`0.0.0.0`, `::`), multicast,
and the limited-broadcast address are rejected. Prometheus's scrape target can
use a DNS name resolving to that interface; bracket IPv6 targets, for example
`[2001:db8::10]:9464` (replace this documentation-only address).

Prometheus initiates the connection. Allow only its server address through the
workstation firewall to the selected port. A remote or containerized Prometheus
must have a route to the selected workstation address; its own `127.0.0.1` is not
that workstation. Changing the bind address does not configure routes, NAT,
VPNs, container networking, or firewalls.

Bearer authentication controls access but does **not** encrypt the HTTP token
or metrics. Direct HTTP is for a trusted private LAN or an encrypted VPN.
For other networks, use an existing HTTPS proxy with a certificate Prometheus
verifies, set `scheme: https`, and point the scrape target at that proxy. Use
Prometheus `tls_config.ca_file` for a private CA; do not disable certificate
verification. Traceonaut does not provide native TLS or deploy a proxy/VPN.
Do not expose the plain HTTP endpoint to the internet. See
[Prometheus security guidance](https://prometheus.io/docs/operating/security/) and
[scrape TLS configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#tls_config).

The optional CWO dispatch endpoint and its query client remain loopback-only;
the session collector's remote binding does not change those contracts.

Prometheus reads `credentials_file` from its own filesystem. If it runs as
another user or in a container, supply a protected copy of the same token that
its service user can read. Do not loosen the collector's credential permissions.

The collector requires:

- An owned regular credential file, mode `0600`, with no symlinks. After trimming,
  the token must be 16–4,096 printable ASCII bytes without spaces or newlines.
- Ancestors owned by the current user or root and not group/world-writable,
  except root-owned sticky directories such as `/tmp`.
- Private state and snapshot directories, mode `0700`; snapshot files `0600`.
  State and output must be outside the Codex source profile.
- One collector writer per state directory.

Rotate all token copies together and restart the collector, which reads its
credential at startup. Reload/restart Prometheus as required by your setup.
Grafana needs access to Prometheus, not to the collector token or private database.

## Keep the collector running

Use your existing service manager with the collector command from
[setup](deployment.md#1-run-the-collector). Set its working directory and use
absolute paths for the interpreter, script, source, state, snapshot, and token.
Run it as the user who owns the Codex profile. Traceonaut does not install a
service manager or provide a service installer.

Do not run a foreground collector and a service against the same state directory.
Stopping collection does not require deleting source files or the index.
When the workstation sleeps or disconnects, Prometheus marks the target down.
Stored samples remain; missed scrape intervals are not backfilled from files.

## Pinned releases and upgrades

For a long-running service, build a separate immutable bundle so checkout edits
cannot change its code. From the checkout root:

```bash
export TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
TRACEONAUT_COLLECTOR_RELEASE="$(python3 scripts/build_release.py \
  --component sessions --output-dir "$TRACEONAUT_RELEASES_DIR")"
```

Use `$TRACEONAUT_COLLECTOR_RELEASE/scripts/collect_codex_sessions.py` in the
service command instead of the checkout script. Keep existing state paths and
credentials. `sessions` and `account` include both collectors and their helpers;
`stable`, `beta`, `unified`, and `dispatch` are separate component bundles.

Bundles include a license and hash manifest, but not tests or docs. Existing
bundles are verified rather than overwritten.

Before upgrading, save the current release path and service configuration.
Build the replacement, change the affected service path, restart it, and check
Prometheus and Grafana. If it fails, restore the previous path/configuration.
Keep the database intact. Update session and account services together when
they share a release. Dashboard-only changes do not require a collector restart.

## Automatic dashboard name updates

Manual imports keep metric values current but freeze name mappings at import
time. If you want names and selectors to follow new sessions automatically,
use your existing Grafana file-provisioning workflow and run a renderer watcher.

Keep provisioned dashboard files in a directory separate from the private
snapshot. Example for Beta, from the checkout root:

```bash
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
TRACEONAUT_DASHBOARD_DIR="$TRACEONAUT_DATA_DIR/dashboards"
install -d -m 700 "$TRACEONAUT_DASHBOARD_DIR"
python3 scripts/render_codex_beta_dashboard.py \
  --template examples/observability/grafana-codex-beta-dashboard.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --datasource-uid "replace-with-existing-prometheus-uid" \
  --output "$TRACEONAUT_DASHBOARD_DIR/beta.json" --watch-seconds 2
```

Point a dedicated Grafana file provider at that output directory. Use the actual
datasource UID because provisioning does not show the import-time picker.
Keep the renderer, helper scripts, and template together in a pinned
`--component beta` release for persistent operation. Run the watcher through your
service manager.

For a different Grafana user or container, supply an access-controlled copy of
the rendered file through your existing deployment workflow. Do not grant
Grafana access to the private snapshot or weaken its permissions. Rendered JSON
contains display names, so keep Grafana and the files access-controlled.

To switch from manual import, back up the existing definition and use the same
dashboard UID without configuring duplicate file providers. See
[Grafana file provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards).
Provisioned files can overwrite UI edits. Provider `disableDeletion` controls
whether removing a source file also removes the dashboard.

## Check endpoint authentication

This optional check hides the token, disables proxies/redirects, and reports
only endpoint readiness. It does not print session metrics. First run
`export TRACEONAUT_METRICS_CREDENTIAL="/absolute/path/to/traceonaut/metrics.token"`
with the protected token path used in setup. Replace the URL below with the
selected bind address, or the HTTPS proxy URL; bracket IPv6 addresses.

<!-- metrics-check -->
```bash
export TRACEONAUT_METRICS_URL="http://127.0.0.1:9464/metrics"
python3 - <<'PY'
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

token = Path(os.environ["TRACEONAUT_METRICS_CREDENTIAL"]).read_text(encoding="ascii").strip()
request = Request(os.environ["TRACEONAUT_METRICS_URL"], headers={"Authorization": "Bearer " + token})
try:
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=4) as response:
        payload = response.read()
except HTTPError as error:
    raise SystemExit(f"Metrics HTTP {error.code}; see Troubleshoot below") from None
except URLError:
    raise SystemExit("Metrics connection failed; check listener, network access, and certificate trust") from None
if b"cwo_codex_collector_scan_timestamp_seconds" not in payload:
    raise SystemExit("Expected collector metrics are absent")
print("Authenticated metrics: HTTP 200; collector health metrics present")
PY
```

An initial `503` means no payload is ready. Retry after the first scan finishes.
A `200` proves the endpoint responds, not that source collection is healthy.
Requests without the correct bearer credential must return `401`.

For backlog and source status, inspect:

```promql
cwo_codex_collector_source_available
cwo_codex_collector_pending_files
time() - cwo_codex_collector_scan_timestamp_seconds
```

`source_available` should be `1`; a large initial backlog can take several
bounded passes. Prometheus history starts at the first scrape, not at the dates
of imported session records.

## Troubleshoot

| Symptom | Check |
| --- | --- |
| Collector exits | Source/state permissions, one writer, and a numeric bind address assigned to the workstation. Wildcard binds are rejected. |
| Connection refused or target DOWN | Workstation awake/online, selected bind address, routing/firewall, port, and Prometheus credential-file access. |
| HTTP 401 | Missing or mismatched token. Check protected copies without printing their contents. |
| HTTP 503 | First scan has not produced a payload. If persistent, check source and state access. |
| UP but no session data | Check source availability, scan age, pending files, time range, and session retention. |
| Dashboard names missing/stale | Generate fresh JSON from the current snapshot and re-import, or check the optional watcher/provider. |
| Grafana has no data | Select the correct Prometheus datasource during import; check queries and filters. |
| Account allowance unavailable | The [account reader](codex-beta-dashboard.md#account-allowance) is optional and separate. |
