---
slug: /dashboards/ibm-bob-beta
title: IBM Bob · Beta
description: Bob Shell chats, token usage and tool results in a compact terminal-style Grafana view.
---

# IBM Bob · Beta

![IBM Bob beta with synthetic example data](../../assets/screenshots/ibm-bob-beta.png)

*Example data. No personal chats are shown.*

View Bob Shell chats, token usage and tool results. Compact status tiles and tool
bars leave room for token charts and the full-width Chats table.

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
| Overview | Compact colored tiles for collection status, scan age, selected chats, chats with saved token counts, recorded tokens and file coverage. |
| Input tokens | Separate saved-history and partial captured-generation input totals over Prometheus history. |
| Output tokens | Separate saved-history and partial captured-generation output totals on a separate scale. |
| Responses by chat | The eight selected chats with the most saved assistant responses. |
| Tool activity | Horizontal count bars: results in blue, errors in red, unknown outcomes in orange. |
| Chat activity | Chats with a saved message in the preceding five minutes. |
| Chats | Chat, project, last message, saved tokens, captured tokens, responses, tool errors and both availability states. |
| Captured generation tokens · partial | Capture state, captured ledger tokens, approximate interval tokens, ledger epoch and generation records missing counts. |

A chat is a conversation, subtask or subagent.
Tool errors reflect Bob's saved outcome, including errors from tools other than
the shell.

Tool bars use a shared automatic count scale, not percentages. Errors and unknown
outcomes are included in Results; the bars are not slices to add together.
Collection and file coverage use green for available/complete, orange for partial
file coverage, and red for unavailable/stale. Capture stays explicitly partial.

## Read the Values

[Retention and Limits](../reference/retention-and-limits.md#ibm-bob) explains how
the selected range, older work and export selection affect totals. **K**, **Mil**
and **Bil** mean thousand, million and billion.

**Chats with token counts** counts selected chats with both input and output
counts, including saved zeros. Compare it with **Chats**: if the counts differ,
**Recorded tokens** includes only the chats whose totals are available. Input and
output charts each show their known values independently.

The **Input tokens** and **Output tokens** charts have separate **Saved history**
and **Captured generation · partial** series. Bob can omit saved fields while
OTel supplies captured counts; those counts appear in the same charts. The series
are never added or substituted for each other. Captured lines show the cumulative
ledger over scrape history, not tokens spent within the selected range. Disabled,
unavailable or stale capture leaves gaps. When neither source has a value, the
chart reports unavailable counts; recorded zero remains zero.

The **Token counts** column explains each chat's total:

| Label | Meaning |
| --- | --- |
| Recorded | Bob saved valid input and output counts. |
| Not recorded by Bob | Bob's saved chat omits an input or output count. Responses and tool activity remain available. |
| Unavailable | A saved value is invalid, a parent total cannot safely exclude subtasks, or the collector predates token-status reporting. |
| Unavailable / stale | Check Collection and Scan age before using the retained values. |

Missing saved values appear as **Not recorded**, and missing captured values as
**No captured count**; recorded zeros appear as **0**. Missing token
fields alone leave **File collection coverage** Complete. Bob 2.0.5 can save chat costs
without token counts; Traceonaut leaves those counts absent rather than estimating them.
[Collector Health](../reference/collector-health.md#ibm-bob) explains Overview's
**Collection**, **Scan age** and **File collection coverage**. If collection fails, tables retain
the last collected values and Overview shows unavailable or stale. Charts leave gaps.

Model, effort, currency cost and account allowance are unavailable in this beta.
Re-render and re-import to refresh chat names, or use
[Automatic Name Updates](../operations/automatic-name-updates.md) with the Bob renderer.

## Optional Captured Tokens

Enable [generation capture](../bob-collection.md#optional-generation-token-capture)
to populate **Captured tokens**. These are generation-only counts within a durable
ledger epoch, not restored database history or all Bob usage. Keep them separate
from **Saved tokens**. Summaries, auxiliary calls, compaction and generations
without valid qualified counts are excluded; resumed and parent chats may be partial.

| Capture label | Meaning |
| --- | --- |
| Capture disabled | The optional reader is off; activity and saved counts still work. |
| No activity | No saved message or captured generation is known for the selected chat. |
| Recorded zero · partial | Explicitly captured zero input/output; the supported scope is still incomplete. |
| Missing counts | Activity or a generation is known, but no valid captured total is available. |
| Partial capture | Known counts are displayed; excluded calls, missing fields or capture gaps remain possible. |
| Capture unavailable / stale | Journal access failed or its successful-check timestamp is at least 90 seconds old. Retained ledger values are historical observations. |

Project/Chat filters select capture values. Captured ledger totals are cumulative
and independent of the dashboard's time range; the Chats table still selects chats
by saved activity in that range. **Interval tokens · approximate** uses
Prometheus `increase` over the selected range and scrape/commit time. It needs
enough scrape samples, can be fractional, and does not assign usage to exact model
event times. Disabled capture withholds numeric fields; an absent epoch displays
**No capture epoch**. **Generation records missing counts** counts observed missing records,
not all uncaptured calls or lost tokens.

Collector availability and saved-file Coverage never imply complete token capture.
Receiver health alone cannot establish delivery or completeness. No capture state
is labelled Complete, and no historical counts are recovered by enabling it.
