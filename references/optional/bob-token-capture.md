---
slug: /optional/bob-token-capture
title: Token Capture
description: Send Bob generation counts through your existing OTel Collector.
---

# Token Capture

Start with [IBM Bob collection](../sources/ibm-bob.md). This optional path uses Bob
Shell **2.0.5** and `otel/opentelemetry-collector-contrib:0.161.0`:

```text
Bob → OTel Collector → private journal → Traceonaut → Prometheus → Grafana
```

Bob pushes traces; Prometheus pulls Traceonaut's existing endpoint.
Read the [captured-token scope](../reference/retention-and-limits.md#token-capture)
before enabling it.

## 1. Configure the Collector

From the Traceonaut checkout, set the path to your existing OTel Compose directory.
For rootless Podman, prepare a private journal readable by the ordinary host user.
These commands require `setfacl`:

```bash
export TRACEONAUT_CHECKOUT="$PWD"
export OTEL_COMPOSE_DIR="/absolute/path/to/otel-collector"
export BOB_OTEL_JOURNAL_DIR="$HOME/.local/share/traceonaut/bob-journal"
install -d -m 700 "$BOB_OTEL_JOURNAL_DIR"
podman unshare chown 10001:10001 "$BOB_OTEL_JOURNAL_DIR"
podman unshare setfacl -m 'u:0:r-x,g::---,o::---,m::rwx' \
  -m 'd:u::rwx,d:u:0:r-x,d:g::---,d:o::---,d:m::rwx' "$BOB_OTEL_JOURNAL_DIR"
```

Back up your existing configuration. Copy the supplied
[Collector config](../../examples/observability/bob-otel-collector.yaml) and
[Compose override](../../examples/observability/bob-otel-compose.override.yaml)
alongside `compose.yaml`:

```bash
cp "$TRACEONAUT_CHECKOUT/examples/observability/bob-otel-collector.yaml" "$OTEL_COMPOSE_DIR/"
cp "$TRACEONAUT_CHECKOUT/examples/observability/bob-otel-compose.override.yaml" "$OTEL_COMPOSE_DIR/"
```

Compare the copied config with your current Collector config and preserve your
metrics-pipeline settings. Keep OTLP host ports on loopback, then apply it:

```bash
cd "$OTEL_COMPOSE_DIR"
podman-compose -f compose.yaml -f bob-otel-compose.override.yaml config
podman-compose -f compose.yaml -f bob-otel-compose.override.yaml up -d otel-collector
```

`up` creates or recreates the Collector to apply the pipeline and mounts.
Use a dedicated journal directory separate from source profiles, state and credentials.

## 2. Enable Bob Export

Enable Bob's `telemetry.enabled` setting. This also enables its separate IBM
exporter. The four exclusion flags below filter the external export; titles,
workspace/user metadata and authentication headers can still reach that receiver.
Use a trusted local endpoint. The supplied Collector config removes private
metadata before writing the journal; keep raw/debug exporters disabled.
See [Data Sources and Privacy](../reference/data-sources-and-privacy.md#ibm-bob).

Run Bob with these variables scoped to its invocation:

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

## 3. Read the Journal

Restart your Bob-enabled Traceonaut collector with this additional option:

```bash
--bob-otel-journal-dir "$BOB_OTEL_JOURNAL_DIR"
```

Set `BOB_OTEL_JOURNAL_DIR` in that terminal to the same host directory from step 1.

## 4. Check the Counts

After a new Bob generation and Prometheus scrape, run:

```promql
sum(traceonaut_bob_session_captured_tokens_total{token_kind="total",event_kind="generation"})
```

Expect a numeric count when Bob exported usable usage for a known chat.
See **Captured tokens** and **Capture** in [IBM Bob · Beta](../dashboards/ibm-bob-beta.md).
Use [capture health](../reference/collector-health.md#token-capture) if counts are absent.

## Rollback

1. Stop using the scoped Bob OTel variables and restore its previous telemetry setting.
2. Restart Traceonaut without `--bob-otel-journal-dir`.
3. Restore the previous Collector configuration and recreate it without the capture override:

```bash
cd "$OTEL_COMPOSE_DIR"
podman-compose -f compose.yaml up -d otel-collector
```

Keep private state and Prometheus history. Database collection continues to provide
chat activity and saved counts.
