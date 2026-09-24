---
slug: /integrations/observed-job-runner
title: Observed Job Runner
description: Launch authorized read-only Codex jobs and record their dispatch metrics.
---

# Observed Job Runner

The [Observed Job Runner](../../scripts/run_observed_codex.py) starts one or two fresh Codex jobs and records their metrics for [CWO Dispatches](../dashboards/cwo.md).

**Running it starts model work and can incur usage.** Obtain authorization for the tasks before preparing the files below.

## Requirements

- Codex CLI **0.154.0** on `PATH`, with an existing login. The runner checks this exact version.
- An [observation configuration](cwo.md#connect-a-controller) that allows `standard_codex` and the chosen model and effort.
- An upstream connection for the model. Job tools run read-only with network access disabled.

## Prepare the Job

Save a private `0600` manifest at an absolute path. Replace the placeholders with a fresh job UUID, your checkout, allowed settings and authorized task:

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

For readable dashboard names, supply `task_name` and `agent_name` together. `work_item_title` is optional. Use short, single-line display labels; keep prompts, secrets and Grafana variable expressions out of them.

## Record the Authorization

Save a separate `0600` authorization JSON file. It records the user's prior approval and binds it to these exact inputs:

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

## Run

The runner serves its own metrics endpoint. Stop any standalone exporter using the same address and port before starting it.

From the checkout root or dispatch release directory:

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

To save dashboard names, set `CWO_PRESENTATION_FILE` to an absolute registry path in a private `0700` directory. Add both `--presentation-file "$CWO_PRESENTATION_FILE"` and `--project-name "Example project"`. Use non-sensitive names.

## Check the Result

Read each job's status, timeout, cleanup and observation fields. `runner_status: completed` describes the runner; individual jobs and telemetry delivery have their own outcomes.

The linger period keeps the endpoint available for final scrapes. Confirm the final samples reached Prometheus before relying on them. Restart the standalone exporter afterward if you use one.

Saved output contains metadata and receipts. Model answers are not saved. See [Limits and Internals](../reference/limits-and-internals.md#cwo-embedding-and-recovery) for final-sample confirmation and recovery.
