---
slug: /dashboards/ibm-bob-beta
title: IBM Bob · Beta
description: Bob Shell chats, token usage and tool results in a compact terminal-style Grafana view.
---

# IBM Bob · Beta

![IBM Bob beta with synthetic example data](../../assets/screenshots/ibm-bob-beta.png)

*Example data. No personal chats are shown.*

View Bob Shell chats, token usage and tool results in a compact, two-column
dashboard based on [Codex TUI · Beta](tui-beta.md).

## Import the Dashboard

Enable [IBM Bob Collection](../bob-collection.md), then run from the checkout root:

<!-- render-bob -->
```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_bob_dashboard.py \
  --template examples/observability/ibm-bob-beta.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --output "$TRACEONAUT_DATA_DIR/ibm-bob-beta.json"
```

In Grafana, open **Dashboards → New → Import**, upload `ibm-bob-beta.json`, and select
your Prometheus datasource. The title is **IBM Bob · Beta**.

## Use the Dashboard

Choose a **Project**, **Chat** and time range. Usage and activity panels share these
filters. Collection health covers the whole Bob source. The default range is 24
hours with a 30-second refresh.

| Section | Shows |
| --- | --- |
| Overview | Collection status, scan age, selected chats, chats with token counts, recorded tokens and coverage. |
| Input tokens | Recorded input totals over Prometheus history. |
| Output tokens | Recorded output totals on a separate scale. |
| Responses by chat | The eight selected chats with the most saved assistant responses. |
| Tool activity | Saved tool results, explicit errors and unknown outcomes. |
| Chat activity | Chats with a saved message in the preceding five minutes. |
| Chats | Chat, project, last message, tokens, responses, tool errors and token availability. |

A chat is a conversation, subtask or subagent.
Tool errors reflect Bob's saved outcome, including errors from tools other than
the shell.

## Read the Values

[Retention and Limits](../reference/retention-and-limits.md#ibm-bob) explains how
the selected range, older work and export selection affect totals. **K**, **Mil**
and **Bil** mean thousand, million and billion.

**Chats with token counts** counts selected chats with both input and output
counts, including saved zeros. Compare it with **Chats**: if the counts differ,
**Recorded tokens** includes only the chats whose totals are available. Input and
output charts each show their known values independently.

The **Token counts** column explains each chat's total:

| Label | Meaning |
| --- | --- |
| Recorded | Bob saved valid input and output counts. |
| Token counts not recorded by Bob | Bob's saved chat omits an input or output count. Responses and tool activity remain available. |
| Token counts unavailable | A saved value is invalid, a parent total cannot safely exclude subtasks, or the collector predates token-status reporting. |
| Collection unavailable / stale | Check Collection and Scan age before using the retained values. |

Missing numeric values appear as **—**; saved zeros appear as **0**. Missing token
fields alone leave overall Coverage **Complete**. Bob 2.0.5 can save chat costs
without token counts; Traceonaut leaves those counts absent rather than estimating them.
[Collector Health](../reference/collector-health.md#ibm-bob) explains Overview's
**Collection**, **Scan age** and **Coverage**. If collection fails, tables retain
the last collected values and Overview shows unavailable or stale. Charts leave gaps.

Model, effort, currency cost and account allowance are unavailable in this beta.
Re-render and re-import to refresh chat names, or use
[Automatic Name Updates](../operations/automatic-name-updates.md) with the Bob renderer.
