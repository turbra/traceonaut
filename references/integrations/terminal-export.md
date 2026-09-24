---
slug: /integrations/terminal-export
title: Completed Dispatch Export
description: Export completed CWO observations as private JSON for offline analysis.
---

# Completed Dispatch Export

Read an existing [CWO ledger](cwo.md) from the checkout root:

```bash
python3 scripts/export_terminal_observations.py \
  --state-dir /absolute/path/to/private-dispatch-ledger \
  --output-dir /absolute/path/to/separate/private-output
```

The output directory is separate from the ledger. The exporter creates private directories/files and rejects unsafe permissions or overlapping paths.

## Output

Each `terminal_dispatch_projection.v1` record includes identity, requested/acknowledged configuration, available token values, outcome, elapsed time, allowances and collection health.

A job is exported after it is terminal, its bindings/connections are closed and queues are drained. Failed and interrupted jobs can still have useful observations. Missing values are null; shared health is marked separately from job-attributed health.

Files under `observations/` have stable opaque names. Later corrections update the same logical job. Interrupted writes can be retried without creating duplicate runs.

Prompts, model answers, commands, tool output and free-form errors are excluded. This is an offline read; it starts no model job or listener. [Limits and Internals](../reference/limits-and-internals.md) describes accounting and publication boundaries.
