---
slug: /integrations/cwo
---

# Optional CWO integration

[Complex Work Orchestration (CWO)](https://github.com/gprocunier/complex-work-orchestration)
is a Codex skill for planning complex tasks, coordinating coding agents, and
tracking work across sessions.

Skip this guide for ordinary session monitoring. The session collector, account
reader, and session dashboards work without CWO.

This integration observes jobs (dispatches) launched by a CWO app-server controller. It
adds a private accounting ledger and separate dispatch metrics. Loading the
observer does not attach it to existing sessions or authorize model work.
Ordinary session records cannot establish dispatch ownership.

## Connect an existing controller

From the checkout root, build a separate dispatch release:

```bash
TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
TRACEONAUT_DISPATCH_RELEASE="$(python3 scripts/build_release.py \
  --component dispatch --output-dir "$TRACEONAUT_RELEASES_DIR")"
export PYTHONPATH="$TRACEONAUT_DISPATCH_RELEASE/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 -c 'from traceonaut.observability_host import open_observability_host'
```

CWO's app-server controller accepts `--observability-config` with an absolute
path to a protected JSON file. Use it with an otherwise authorized CWO
invocation. Traceonaut supplies observation components, not the controller.
An unavailable observer must not change worker execution.

The configuration must be an owner-only `0600` regular file:

| Field | Value |
| --- | --- |
| `state_dir` | Absolute private ledger directory, outside the source checkout and Codex profile. |
| `registration` | Fields from the [registration schema](../schemas/supervisor-project-registration-v1.schema.json), without `record_type`. The owning principal must match the process UID and state must be `enabled`. |
| `executor_vocabulary`, `model_vocabulary`, `effort_vocabulary` | Nonempty lists of allowed bounded label values. |
| `source_compatibility_sha256` | SHA-256 reference to compatibility evidence for the selected app-server protocol. Supplying a hash does not validate the runtime. |
| `metrics` | Object with numeric loopback `host`, integer `port`, and absolute `credential_file`. |
| `prometheus` (optional) | Object with loopback `url`, exact scrape `job` and `instance`, and optional protected `credential_file`. |

Registration includes explicit queue, record, registration-count, and disk limits.
Persist project identity, registration generation, and the ledger key together.
The ledger permits one writer; read-only exporters can inspect committed data.
Directories must be `0700`, files `0600`, and paths must not traverse symlinks or
unsafe writable ancestors. Do not regenerate a missing key to resume ingestion:
that would discard the basis for deduplication.

Use a separate endpoint port if the session exporter already uses `9464`.
Credentials follow the [same protected-file rules](operations.md#network-and-credentials).
Never bind the exporter to a public interface.

## Embedding interface and recovery

For an embedding application,
`open_observability_host(Path(config_file))` composes the ledger, adapter,
observer, and metrics service. See the
[host interface](../scripts/traceonaut/observability_host.py) and
[runtime observer](../scripts/traceonaut/observability_runtime.py).

The controller prepares a dispatch with a fresh agent identity, packet hash,
submission reference, and requested configuration. Preparation alone is not an
observed dispatch. The durable submission receipt must precede persistence and
acknowledgement binding. Completion messages are held only within bounded
queues until a unique trusted binding exists; timestamps cannot establish a
binding.

On startup, incomplete source connections become visible data gaps. The host
does not automatically replay controller receipts after a crash. An integrating
controller supplies trusted normalized receipts through
`reconcile_controller_receipts`. Active elapsed time remains unavailable across
an unproven clock boundary.

Close the observer outside worker control, drain its queues, then close the
ledger. Check the boolean result of
`ObservabilityHost.close(timeout_seconds=...)`: `False` means cleanup is still
running. A caller timeout does not by itself count as a lost event.

At capacity, ingestion stops with a visible coverage gap; retained history is
not deleted. Disk admission includes the database and retained sidecars, keys,
and lock files, with a conservative reserve for in-flight writes. Leave
filesystem headroom and measure usable capacity for your workload.

## Read an existing ledger

These commands do not attach to workers or launch work. From the dispatch release
directory, replace the private paths:

```bash
cd "$TRACEONAUT_DISPATCH_RELEASE"
CWO_OBSERVABILITY_STATE="/absolute/path/to/existing/private-ledger"
CWO_OBSERVABILITY_TOKEN_FILE="/absolute/path/to/protected/metrics.token"

python3 scripts/export_dispatch_observability.py \
  --state-dir "$CWO_OBSERVABILITY_STATE" --capacity-report

python3 scripts/export_dispatch_observability.py \
  --state-dir "$CWO_OBSERVABILITY_STATE" \
  --credential-file "$CWO_OBSERVABILITY_TOKEN_FILE" \
  --host 127.0.0.1 --port 9465
```

Configure a separate Prometheus scrape job and port using the
[scrape example](../examples/observability/prometheus-scrape.yaml). Keep the
session job unchanged. The [network and credential boundaries](operations.md#network-and-credentials)
still apply.

The standalone exporter cannot establish the owner's monotonic-clock continuity
or write publication confirmations. Active elapsed time therefore remains
unavailable, and unconfirmed terminal series remain exposed. An embedded
`ObservabilityExportService` can confirm stored revisions through
`PublicationConfirmer` using the exact Prometheus scrape labels. A successful
HTTP scrape alone does not prove the required final samples were stored.

For offline JSON, use [completed dispatch export](terminal-observation-export.md).

## Add the dispatch dashboard

Use the protected name registry maintained by the controller or observed runner.
It supplies display names without adding them to metric labels. From the
dispatch release directory:

```bash
CWO_PRESENTATION_FILE="/absolute/path/to/private/presentation.json"
CWO_GRAFANA_DASHBOARD_FILE="/absolute/path/to/provisioned/dispatch.json"
CWO_PROMETHEUS_DATASOURCE_UID="replace-with-datasource-uid"

python3 scripts/render_observability_dashboard.py \
  --template examples/observability/grafana-dashboard.json \
  --presentation-file "$CWO_PRESENTATION_FILE" \
  --datasource-uid "$CWO_PROMETHEUS_DATASOURCE_UID" \
  --output "$CWO_GRAFANA_DASHBOARD_FILE" --watch-seconds 2
```

**Do not provision this template and the Stable session dashboard together.**
Both use UID `cwo-supervisor-observability-v1`. Beta and Unified have separate
UIDs. Without the name registry, dispatch names remain unavailable.

Dispatches, agents, and completed responses are different units. Requested and
acknowledged settings are not actual response-model evidence. Token totals can
be partial or unavailable; cached input and reasoning output are subsets, not
additional usage. Declared allowances are display-only and do not change
enforcement. No raw reasoning, ETA, or billing is inferred.

## Optional observed-job runner

`scripts/run_observed_codex.py` launches one or two fresh read-only jobs and
records their dispatch telemetry. Unlike the exporters above, **this starts
model work and can incur usage**. It observes only its own jobs, not the
surrounding conversation.

The runner requires Codex CLI **0.154.0** on `PATH` with an existing login. It
rejects other versions. Tool network access is disabled, but the app-server
still needs its upstream model connection. The observation configuration must
admit `standard_codex` and the selected model/effort.

Prepare an absolute-path `0600` manifest. Replace the placeholders with a fresh
job UUID, your checkout, allowed settings, and the authorized task:

```json
{
  "version": 1,
  "timeout_seconds": 180,
  "jobs": [{
    "job_id": "<fresh-job-uuid>",
    "cwd": "/absolute/path/to/checkout",
    "model": "<allowed-model>",
    "effort": "<allowed-effort>",
    "prompt": "<authorized-read-only-task>"
  }]
}
```

Optional `task_name` and `agent_name` must be supplied together;
`work_item_title` is also optional. Names are bounded single-line display text,
not prompts or secrets. Do not use Grafana variable expressions in names.

A separate `0600` authorization JSON file binds the existing decision and exact
inputs. It is not an independent grant of permission:

| Field | Required value |
| --- | --- |
| `version`, `authorization_id` | `1` and a fresh UUID. |
| `route` | `standard_codex`. |
| `authority_kind`, `authority_scope` | `explicit-user-authorization`, `standard-codex-observability`. |
| `authority_decision_sha256` | SHA-256 of the recorded authorization decision. |
| `manifest_sha256`, `observability_config_sha256` | SHA-256 of the exact file bytes. |
| `allowed_jobs` | One object per job with exact `job_id`, `cwd`, `model`, `effort`, and SHA-256 of its UTF-8 prompt as `prompt_sha256`. |
| `limits` | `max_concurrency` of 1 or 2, `max_wall_seconds` covering the manifest timeout, and `max_linger_seconds` covering the requested linger. |
| `sandbox`, `network_access` | `read-only`, `false`. |
| `native_attestation_claimed`, `native_policy_override` | Both `false`. |

From the dispatch release directory, replace the paths to those prepared files:

```bash
CWO_OBSERVABILITY_MANIFEST="/absolute/path/to/private/jobs.json"
CWO_OBSERVABILITY_AUTHORIZATION="/absolute/path/to/private/authorization.json"
CWO_OBSERVABILITY_CONFIG="/absolute/path/to/private/observability.json"
CWO_OBSERVABILITY_RECEIPTS="/absolute/path/to/private/receipts"
install -d -m 700 "$CWO_OBSERVABILITY_RECEIPTS"
python3 scripts/run_observed_codex.py \
  --manifest "$CWO_OBSERVABILITY_MANIFEST" \
  --authorization-file "$CWO_OBSERVABILITY_AUTHORIZATION" \
  --observability-config "$CWO_OBSERVABILITY_CONFIG" \
  --receipt-dir "$CWO_OBSERVABILITY_RECEIPTS" --linger-seconds 30
```

To populate a name registry, supply names in the manifest and add
`--presentation-file "$CWO_PRESENTATION_FILE" --project-name "Example project"`.
These options must be supplied together. The registry belongs in a private
`0700` directory and must not contain sensitive descriptions.

The runner owns its metrics endpoint while executing. Stop any standalone
exporter using that endpoint first, then restart it afterward if needed. The
linger period permits final scrapes but does not guarantee publication.
Inspect job statuses, timeout, cleanup, and observation state, not just
`runner_status: completed`. The runner records metadata and receipts, not model
answer text.
