---
slug: /integrations/observed-job-runner
title: Observed Job Runner
description: Run explicitly authorized read-only jobs with dispatch observation.
---

# Observed Job Runner

[scripts/run_observed_codex.py](../../scripts/run_observed_codex.py) launches one or two fresh read-only jobs and
records their dispatch telemetry. **This starts model work and can incur usage.**
Its observations cover the jobs it launches.

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

From the checkout root or dispatch release directory, replace the paths to those prepared files:

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
