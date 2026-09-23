# Export completed dispatch metadata

This optional export reads the [CWO dispatch ledger](cwo-integration.md) and writes
private JSON observations for offline analysis. It is not needed for session
collection and does not read ordinary Codex rollout files.

From the checkout root, replace the two paths:

```bash
CWO_STATE_DIR="/absolute/path/to/existing/private-dispatch-ledger"
CWO_TERMINAL_OUTPUT="/absolute/path/to/separate/private-terminal-output"
python3 scripts/export_terminal_observations.py \
  --state-dir "$CWO_STATE_DIR" \
  --output-dir "$CWO_TERMINAL_OUTPUT"
```

Use an existing protected ledger and a separate output path with a safely owned
parent directory. Paths must not overlap. The exporter creates `0700`
directories and `0600` files, and rejects unsafe permissions and symlinks.

## What gets exported

A dispatch is eligible only after its lifecycle is terminal, its bindings are
closed, source connections are disconnected, and pending records and queues are
drained. Failed or interrupted jobs can still have useful telemetry.

Each `terminal_dispatch_projection.v1` record contains:

- Project/dispatch identity, source revision, and accounting revision.
- Requested and acknowledged configuration with provenance. Actual response
  model and effort remain unavailable.
- Nullable token values, contribution counts, completed cycles, and field states.
- Terminal outcome, available elapsed time, declared allowances, and separate
  enforcement limits.
- Project-scoped collection health and whether it can be attributed to this run.

Missing usage is not zero. Shared or unproven health remains visible but cannot
establish a clean run comparison. These records are not billing data.

## Consistency and privacy

All database fields come from one committed read transaction. Filesystem size
is observation-time metadata, not revision-exact accounting. Prometheus
publication is reported separately and is not required for export.

Files under `observations/` use stable opaque names. Late accounting or
attributable health corrections update the same logical run. The cursor advances
only after every observation is durably written, so replay after a failed write
does not create extra runs. Older revisions and conflicting content at the same
source revision are rejected.

The output excludes prompts, responses, commands, tool output, and free-form
errors. This command starts no listener or model job and requires no extra
Python packages.
