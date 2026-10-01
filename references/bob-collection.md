---
slug: /bob-collection
title: IBM Bob Collection
description: Collect Bob Shell sessions alone or alongside Codex on one metrics endpoint.
---

# IBM Bob Collection

Traceonaut reads Bob Shell's local `db/bob.db`. Prometheus scrapes the collector's
metrics endpoint, and Grafana displays the results in [IBM Bob · Beta](dashboards/ibm-bob-beta.md).
Collect Bob only, Codex only, or both through the same endpoint. Each source is
collected separately; a failure in one leaves the other running.

Tested with **Bob Shell 2.0.1** on Linux. The Bob reader needs access to the local
database, with no account credentials or API calls.

## Prepare

From the [installed checkout](install.md), set the paths for the sources you use and
create private collector storage. Run as the user who owns those profiles.

```bash
export TRACEONAUT_BOB_HOME="$HOME/.bob"      # For Bob collection
export TRACEONAUT_CODEX_HOME="$HOME/.codex"  # For Codex collection
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
install -d -m 700 "$TRACEONAUT_DATA_DIR"
```

Use absolute source paths and keep collector storage outside the source profiles.
If replacing an existing collector, stop it before running these checks. Reuse its
state directory, snapshot paths and token.

## Check the Source

Choose **one** command below. `--once` reads the source and exits without serving
metrics. The output reports each enabled source separately; expect
`source_available: 1`. For Bob, `pending: 1` means more records remain to be read.
The continuous collector finishes that work on later scans. `collection_complete: 1`
means the scan finished without known gaps.

### Bob Only

<!-- bob-only-once -->
```bash
python3 scripts/collect_sessions.py \
  --bob-home "$TRACEONAUT_BOB_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --bob-snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --once
```

Codex does not need to be installed. Only Bob metrics and state are created.

### Codex Only

<!-- codex-only-once -->
```bash
python3 scripts/collect_sessions.py \
  --codex-home "$TRACEONAUT_CODEX_HOME" \
  --session-state-dir "$TRACEONAUT_DATA_DIR/session-state" \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --once
```

Bob does not need to be installed. Existing `collect_codex_sessions.py` commands
continue to work unchanged.

### Both

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
python3 scripts/create_metrics_token.py \
  --credential-file "$TRACEONAUT_DATA_DIR/metrics.token"
```

Remove `--once` from your chosen command and add:

```text
--credential-file "$TRACEONAUT_DATA_DIR/metrics.token" \
  --host 127.0.0.1 --port 9464
```

Leave the collector running; **Ctrl+C** stops it. The default address above is for
Prometheus on the same machine. For remote or container Prometheus, replace
`127.0.0.1` with the workstation's numeric LAN or VPN address in both the collector
command and scrape target. Follow [Network and Security](operations/network-and-security.md).

For persistent collection, use [Run as a Service](operations/run-as-a-service.md)
with your chosen command.

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

Import [IBM Bob · Beta](dashboards/ibm-bob-beta.md) for Bob. Import Codex dashboards
only when you collect Codex. Run the renderer in a second terminal while the
collector remains running.

## Disable a Source

Stop the collector, remove that source's home and snapshot arguments, then restart.
The collector stops reading that source and serving its metrics. Its saved state
and Prometheus history remain. To disable Codex, also remove any account or CWO
options.

## Data and Limits

Bob exports recorded task tokens, saved response counts, tool results/errors and
message activity. Completed subtasks are removed from parent totals before summing;
cached tokens are already included in input. Missing values stay missing.
Model, effort, currency cost and account allowance are outside this beta's data.

The default [retention window and export cap](reference/retention-and-limits.md#ibm-bob)
apply separately to each source. Reading older Bob records supplies recorded totals;
Prometheus charts still begin at the first scrape.
