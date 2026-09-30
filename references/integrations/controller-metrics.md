---
slug: /integrations/controller-metrics
title: Controller Metrics
description: Configure the dispatch-ledger exporter and controller embedding interface.
---

# Controller Metrics

<a id="existing-controller-metrics"></a>

For developers integrating a CWO task controller: a controller assigns tasks to agents, and the ledger is its stored task database. Existing integrations can write records to Traceonaut's `observability.sqlite3` database and expose `cwo_dispatch_*` and `cwo_cycle_*` metrics. CWO Overview uses session, audit, and review sources. See the [metric definitions](../reference/metrics.md#cwo-dispatches).

## Export an Existing Ledger

Append `--state-dir /absolute/path/to/private-ledger` to the session collector command to expose dispatch metrics on the existing endpoint.

For a separate endpoint, run from the checkout root:

```bash
python3 scripts/export_dispatch_observability.py \
  --state-dir /absolute/path/to/private-ledger \
  --credential-file /absolute/path/to/private/dispatch.token \
  --host 127.0.0.1 --port 9465
```

Set port `9465` explicitly alongside the session collector, which also uses `9464`. Enable the commented `traceonaut-dispatches` job in the [scrape example](../../examples/observability/prometheus-scrape.yaml) only for this separate endpoint. The standalone endpoint is loopback-only; use the shared session endpoint for the session collector's supported remote-scrape layout.

`--once` prints one metrics snapshot and exits. `--capacity-report` prints local ledger and series counts. Both read committed state without starting jobs.

## Embed the Observer in a Controller

Build the observation components:

```bash
TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
TRACEONAUT_DISPATCH_RELEASE="$(python3 scripts/build_release.py --component dispatch --output-dir "$TRACEONAUT_RELEASES_DIR")"
export PYTHONPATH="$TRACEONAUT_DISPATCH_RELEASE/scripts${PYTHONPATH:+:$PYTHONPATH}"
```

This is an embedding interface for controller maintainers. Confirm that the controller supports the observation hook before using `--observability-config`. The configuration is an owned `0600` JSON file at an absolute path:

| Field | Content |
| --- | --- |
| `state_dir` | Absolute private ledger directory, separate from the checkout and Codex profile. |
| `registration` | [Registration schema](../../schemas/supervisor-project-registration-v1.schema.json) fields without `record_type`; matching process UID and `enabled` state. |
| `executor_vocabulary`, `model_vocabulary`, `effort_vocabulary` | Nonempty allowed label values. |
| `source_compatibility_sha256` | Reference to compatibility evidence for the app-server protocol. |
| `metrics` | Numeric loopback `host`, integer `port`, and absolute `credential_file`. |
| `prometheus` (optional) | Loopback `url`, exact scrape `job` and `instance`, and optional protected `credential_file`. |

Use protected directories and preserve the ledger key. Traceonaut supplies the observer; the controller owns orchestration. Enabling the hook does not turn ordinary sessions into dispatch records.

## Export Completed Dispatches

Use [Completed Dispatch Export](terminal-export.md). [Limits and Internals](../reference/limits-and-internals.md#cwo-embedding-and-recovery) covers embedding, recovery, and final-sample confirmation.
