---
slug: /integrations/terminal-export
title: Completed Dispatch Export
description: Save finished CWO-controller task records as private JSON files for offline analysis.
---

# Completed Dispatch Export

Completed Dispatch Export is an optional command-line utility that saves finished CWO task records as private JSON files. A **dispatch** is a task that a CWO controller assigned to an agent.

Use it to analyse recorded task outcomes, token usage and execution limits outside Grafana. **It is not a dashboard or a Grafana panel.**

This utility reads an existing `observability.sqlite3` database created by a [controller observation integration](controller-metrics.md). It does not read ordinary Codex session history. If you only use Traceonaut's session collector and dashboards, you can skip this page.

## Export Task Records

From the checkout root:

```bash
python3 scripts/export_terminal_observations.py \
  --state-dir /absolute/path/to/private-dispatch-ledger \
  --output-dir /absolute/path/to/separate/private-output
```

Set `--state-dir` to the private directory containing that database. Choose a separate output directory whose parent already exists. The exporter creates owner-only directories and files and rejects unsafe permissions or overlapping paths.

## Output

Example output layout; the observation filename is abbreviated:

```text
private-output/
├── observations/
│   └── <64-character-hash>.json  # One task record
├── cursor.json                 # Tracks export progress
└── export.lock                 # Prevents simultaneous exports
```

Each `terminal_dispatch_projection.v1` JSON record contains:

- Project and task identifiers.
- Requested and acknowledged model and effort settings.
- Available token counts, outcome and elapsed time.
- Declared response/time allowances and enforced execution limits, when recorded.
- Collection health and missing-data indicators.

A task is eligible after CWO records a final state, its source connections and task bindings close, and pending events are processed. Failed and interrupted tasks can be exported too. Missing values are null; requested or acknowledged model settings do not establish the model actually used.

Later corrections update the same task file. Interrupted exports can be retried without creating duplicate task records.

The exporter leaves the source database unchanged and starts no agent or server. Prompts, model answers, commands, tool output and free-form errors are excluded. [Limits and Internals](../reference/limits-and-internals.md) describes accounting and publication boundaries.
