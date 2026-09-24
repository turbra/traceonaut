---
slug: /integrations/cwo
title: CWO Integration
description: Expose metrics from an existing CWO app-server controller or dispatch ledger.
---

# CWO Integration

[Complex Work Orchestration](https://github.com/gprocunier/complex-work-orchestration) is a Codex skill for coordinating agents and tracking work across sessions. This optional integration collects jobs launched through its app-server controller.

## Use an Existing Ledger

The simplest option is to append `--state-dir /absolute/path/to/private-ledger` to the session collector command. This exposes dispatch metrics on the existing endpoint.

For a separate endpoint, from the checkout root:

```bash
python3 scripts/export_dispatch_observability.py \
  --state-dir /absolute/path/to/private-ledger \
  --credential-file /absolute/path/to/private/dispatch.token \
  --host 127.0.0.1 --port 9465
```

Set `--port 9465` explicitly: the CLI's compatible default is `9464`, also used by the session collector. Enable the commented `traceonaut-dispatches` job in the [scrape example](../../examples/observability/prometheus-scrape.yaml) only for this separate endpoint.

`--once` prints one metrics snapshot and exits. `--capacity-report` prints local ledger and series counts. Both read committed state without starting jobs.

The standalone endpoint is loopback-only. Use the shared session endpoint for the session collector's supported remote-scrape layout.

## Connect a Controller

Build the observation components:

```bash
TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
TRACEONAUT_DISPATCH_RELEASE="$(python3 scripts/build_release.py --component dispatch --output-dir "$TRACEONAUT_RELEASES_DIR")"
export PYTHONPATH="$TRACEONAUT_DISPATCH_RELEASE/scripts${PYTHONPATH:+:$PYTHONPATH}"
```

Pass CWO's controller `--observability-config` with an absolute path to an owned `0600` JSON file:

| Field | Content |
| --- | --- |
| `state_dir` | Absolute private ledger directory, separate from the checkout and Codex profile. |
| `registration` | [Registration schema](../../schemas/supervisor-project-registration-v1.schema.json) fields without `record_type`; matching process UID and `enabled` state. |
| `executor_vocabulary`, `model_vocabulary`, `effort_vocabulary` | Nonempty allowed label-value lists. |
| `source_compatibility_sha256` | Reference to compatibility evidence for the app-server protocol. |
| `metrics` | Numeric loopback `host`, integer `port`, absolute `credential_file`. |
| `prometheus` (optional) | Loopback `url`, exact scrape `job` and `instance`, optional protected `credential_file`. |

Use protected directories and preserve the ledger key. Traceonaut supplies the observer; CWO owns the controller. Existing ordinary sessions do not become dispatches by enabling this hook.

## View and Export

Import [CWO Dispatches](../dashboards/cwo.md), or use [Completed Dispatch Export](terminal-export.md). [Limits and Internals](../reference/limits-and-internals.md#cwo-embedding-and-recovery) covers embedding, recovery and final-sample confirmation.

The [Observed Job Runner](observed-job-runner.md) is a separate option that starts explicitly authorized model work.
