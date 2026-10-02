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

Tested with **Bob Shell 2.0.1** on Linux. The Bob reader needs access to the local
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

## Data and Limits

Bob exports recorded chat tokens, saved responses, tool results and recent message
activity. See [Metrics](reference/metrics.md#ibm-bob) for accounting and
[Retention and Limits](reference/retention-and-limits.md#ibm-bob) for history and
export limits. The [IBM Bob dashboard guide](dashboards/ibm-bob-beta.md) explains
the available panels and filters.
