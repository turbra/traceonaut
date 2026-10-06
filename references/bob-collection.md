---
slug: /bob-collection
title: IBM Bob Collection
description: Collect Bob Shell chats alone or alongside Codex on one metrics endpoint.
---

# IBM Bob Collection

Traceonaut reads Bob Shell's local `db/bob.db`. Prometheus scrapes the collector's
metrics endpoint, and Grafana displays the results in [IBM Bob · Beta](dashboards/ibm-bob-beta.md).
Collect Bob alone or alongside Codex through the same endpoint. Each source is
collected separately; a source read failure leaves the other running.

Saved collection was tested with **Bob Shell 2.0.1 and 2.0.5** on Linux. The Bob reader needs access to the local
database, with no account credentials or API calls.

## Prepare

From the [installed checkout](install.md), set the paths for the sources you use and
create private collector storage. Run as the user who owns those profiles.

```bash
export TRACEONAUT_BOB_HOME="$HOME/.bob"
export TRACEONAUT_CODEX_HOME="$HOME/.codex"
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
export TRACEONAUT_METRICS_CREDENTIAL="$TRACEONAUT_DATA_DIR/metrics.token"
export TRACEONAUT_LISTEN_ADDRESS="127.0.0.1"
install -d -m 700 "$TRACEONAUT_DATA_DIR"
```

Use absolute source paths and keep collector storage outside the source profiles.
If replacing an existing collector, stop it before running these checks. Reuse its
state directory, snapshot paths and token.

## Check the Source

Choose one command below. Expect `source_available: 1` under each enabled source
in the JSON output. Bob can also report `pending: 1`, meaning the running collector
will finish reading the remaining records. See the
[collector command reference](reference/scripts.md#collection-and-checks) for
`--once` behavior and exit codes.

### Bob Only

<!-- bob-only-once -->
```bash
python3 scripts/collect_sessions.py \
  --bob-home "$TRACEONAUT_BOB_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --bob-snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --once
```

For Codex alone, follow [Quick Start](getting-started.mdx).

### Bob and Codex

<!-- both-sources-once -->
```bash
python3 scripts/collect_sessions.py \
  --codex-home "$TRACEONAUT_CODEX_HOME" \
  --bob-home "$TRACEONAUT_BOB_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --bob-snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --once
```

If you already collect Codex account or CWO data, keep those options when adding Bob.

## Run the Collector

Create a token once, or reuse your existing token:

```bash
python3 scripts/create_metrics_token.py --credential-file "$TRACEONAUT_METRICS_CREDENTIAL"
```

Choose the matching continuous command. Leave the collector running; **Ctrl+C**
stops it.

### Bob Only

<!-- bob-only-serve -->
```bash
python3 scripts/collect_sessions.py \
  --bob-home "$TRACEONAUT_BOB_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --bob-snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --credential-file "$TRACEONAUT_METRICS_CREDENTIAL" \
  --host "$TRACEONAUT_LISTEN_ADDRESS" --port 9464
```

### Bob and Codex

<!-- both-sources-serve -->
```bash
python3 scripts/collect_sessions.py \
  --codex-home "$TRACEONAUT_CODEX_HOME" \
  --bob-home "$TRACEONAUT_BOB_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --bob-snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --credential-file "$TRACEONAUT_METRICS_CREDENTIAL" \
  --host "$TRACEONAUT_LISTEN_ADDRESS" --port 9464
```

The default address serves Prometheus on the same machine. For remote or container
Prometheus, set `TRACEONAUT_LISTEN_ADDRESS` to the workstation's numeric LAN or VPN
address before starting, and use it in the scrape target. Follow
[Network and Security](operations/network-and-security.md).

For persistent collection, use the matching source example in
[Run as a Service](operations/run-as-a-service.md).

## Connect Prometheus and Grafana

Use one [Prometheus scrape job](getting-started.mdx#2-add-the-prometheus-scrape-job)
for the endpoint. If Prometheus already scrapes your Codex collector at that
address, the same job will also receive Bob metrics.

For Bob, check these queries in Prometheus. Both should return `1`:

```promql
up{job="traceonaut"}
traceonaut_bob_collector_source_available
```

Use your actual job name if it differs from `traceonaut`.

Import [IBM Bob · Beta](dashboards/ibm-bob-beta.md) for Bob. Run the renderer in
a second terminal while the collector remains running.
For new and renamed chats to appear without repeating the import, configure
[Automatic Name Updates](operations/automatic-name-updates.md#run-the-bob-watcher).

## Disable a Source

Stop the collector, remove that source's home and snapshot arguments, then restart.
The collector stops reading that source and serving its metrics. Its saved state
and Prometheus history remain. To disable Codex, also remove any account or CWO
options.

## Optional Generation Token Capture

Bob 2.0.5 can omit token counts from newly saved chats. Its optional OTLP trace
export can provide some counts before persistence removes them. This route is
qualified from the installed producer implementation; it is not a promise that
every Bob model call is exported. Ordinary database collection remains useful
without OTel.

The optional path is **Bob → existing OTel Collector → private journal → existing
Traceonaut collector → Prometheus → Grafana**. Bob pushes traces; Prometheus pulls
the same Traceonaut metrics endpoint used above. No trace database or additional
production Collector is needed. The tested Collector image is
`otel/opentelemetry-collector-contrib:0.161.0`.

### Prepare the Existing Collector

Use `examples/observability/bob-otel-collector.yaml` as an additive candidate for
your existing metrics configuration. It retains the metrics pipeline and adds
stock filter, transform and file-exporter components. Review any local differences
before replacing a configuration. Keep OTLP host ports on loopback; do not expose
the journal. Operational metrics on 8888 and application metrics on 8889 retain
their existing roles; neither is the Bob token ledger.

For rootless Podman with the pinned image's UID 10001, prepare a dedicated journal
directory and give the ordinary host reader read/search access. These commands
require `setfacl`; they affect only the new directory:

```bash
export BOB_OTEL_JOURNAL_DIR="$HOME/.local/share/traceonaut/bob-journal"
install -d -m 700 "$BOB_OTEL_JOURNAL_DIR"
podman unshare chown 10001:10001 "$BOB_OTEL_JOURNAL_DIR"
podman unshare setfacl -m 'u:0:r-x,g::---,o::---,m::rwx' \
  -m 'd:u::rwx,d:u:0:r-x,d:g::---,d:o::---,d:m::rwx' "$BOB_OTEL_JOURNAL_DIR"
```

Place `bob-otel-collector.yaml` and the supplied
`bob-otel-compose.override.yaml` alongside the existing `compose.yaml`. The
override mounts the candidate configuration and this directory. Inspect the
merged configuration before applying it:

```bash
podman-compose -f compose.yaml -f bob-otel-compose.override.yaml config
podman-compose -f compose.yaml -f bob-otel-compose.override.yaml up -d otel-collector
```

`up` creates a missing container or recreates one whose configuration changed.
Starting an existing stopped container alone does not apply the new mounts or
pipeline. Configuration replacement/recreation is a deployment decision.

Rotation retains up to eight 16 MiB backups plus the active file. The host reader
consumes at most 4 MiB per poll, normally every five seconds. Unread backup eviction
loses data; size retention is not a completeness guarantee. The directory's ACL
blocks other users even when the exporter creates files with broader mode bits.
Avoid shared directories, symlinks and hardlinks.

### Opt In for Bob and Traceonaut

Bob's `telemetry.enabled` choice controls both its external exporter and its
separate IBM exporter. Enabling external capture can also enable IBM telemetry;
the flags below do not independently disable IBM delivery. Choose that setting
deliberately in Bob's normal settings before using the integration.

Scope these variables to Bob's invocation:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_TRACES_SAMPLER=always_on \
OTEL_EXCLUDE_USER_PROMPTS=1 \
OTEL_EXCLUDE_ASSISTANT_RESPONSES=1 \
OTEL_EXCLUDE_TOOL_DETAILS=1 \
OTEL_EXCLUDE_TOOL_OUTPUT=1 \
bob
```

Use only a trusted local receiver: Bob can forward authentication/header values
to its external export endpoint. Never put credentials into the Collector YAML or
capture configuration. The exclusion flags apply to the external exporter;
titles, workspace and user metadata can still reach that receiver. The supplied
projection removes them before persistence, retains only hashed chat identity,
trace/span IDs, timestamps, a closed producer-version value and numeric generation
usage, and rejects payload-bearing events/links. Do not add a raw/debug exporter.

Add this option to either continuous Bob collector command above:

```bash
--bob-otel-journal-dir "$BOB_OTEL_JOURNAL_DIR"
```

It requires `--bob-home`. Keep the journal separate from source homes, private
index storage, snapshots and credentials. The reader uses additive tables in the
existing private `bob.sqlite3`; it never writes Bob's source database. Existing
chat/project joins and automatic name updates still come from database collection.

### Read Capture Coverage and Roll Back

Only valid integer input/output on qualified Bob 2.0.5 **LLM Generation** spans
are counted. Cache/reasoning fields are details, not additions. Summaries,
compaction, auxiliary calls and missing/unsupported generation fields are excluded.
Resume and parent generations can lack usable counts even when the provider
returned them. Child counts belong to their own chat; they are not copied into
parent totals. Capture is therefore always an explicitly partial scope.

Saved history, captured ledger totals and interval increases are separate values.
Do not add saved and captured totals. Interval usage follows scrape/commit time,
with Prometheus interpolation, rather than exact event time. Outages, sampling,
producer exit before flush, rotation and unresolved identity joins can permanently
lose usage. Unknown producer versions remain unavailable.

Repeated spans and readable replay do not increase totals. Dedup identities are
retained up to 100,000 events; once full, new events are rejected with visible loss
instead of evicting identities and permitting recounting. Capture admission also
stops if the shared private index approaches 512 MiB; saved collection continues.
Pending joins expire
after the configured session retention window. If the capture index is lost but
its private epoch marker remains, a new epoch starts at EOF with loss reported.
Later replay with event timestamps at or before that new epoch is rejected;
delayed delivery from the old epoch is also lost rather than recounted.
Keep the index and marker together; do not delete them to replay old history.

To roll back, stop using Bob's scoped OTel environment, restore its previous
telemetry choice deliberately, remove `--bob-otel-journal-dir`, and restore the
previous metrics-only Collector configuration. Keep private state and Prometheus
history. Ordinary Bob activity and saved-token collection continue without capture.

## Data and Limits

Bob exports recorded chat tokens, saved responses, tool results and recent message
activity. See [Metrics](reference/metrics.md#ibm-bob) for accounting and
[Retention and Limits](reference/retention-and-limits.md#ibm-bob) for history and
export limits. The [IBM Bob dashboard guide](dashboards/ibm-bob-beta.md) explains
the available panels and filters.
