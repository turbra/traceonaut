---
slug: /dashboards/ibm-bob-beta
title: IBM Bob · Beta
description: Read Bob chats, saved and captured tokens, responses and tool results.
---

# IBM Bob · Beta

![IBM Bob dashboard with populated synthetic charts and chat counts](../../assets/screenshots/ibm-bob-beta.png)

*Actual Grafana panels at 1800 px wide, using synthetic data.*

## Import the Dashboard

Enable [IBM Bob](../sources/ibm-bob.md), then run from the checkout root:

<!-- render-bob -->
```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_bob_dashboard.py \
  --template examples/observability/ibm-bob-beta.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/bob.json" \
  --output "$TRACEONAUT_DATA_DIR/ibm-bob-beta.json"
```

Open **Dashboards → New → Import**, upload `ibm-bob-beta.json`, and choose your
Prometheus datasource. The UID remains `traceonaut-ibm-bob-beta`.
Use [Automatic Name Updates](../operations/automatic-name-updates.md#run-the-bob-watcher)
to keep Project and Chat selectors current.

## Use the Dashboard

Choose **Project**, **Chat** and a time range. Both token charts and the Chats table
select chats whose last saved message falls in that range.

| Panel | Shows |
| --- | --- |
| Overview | Collection status, scan age, chats, chats with saved counts, saved tokens and chat data coverage. |
| Tool activity | Results, errors and unknown outcomes as count bars. Errors and unknown outcomes are included in Results. |
| Input tokens / Output tokens | Saved history and captured tokens for the same selected chats. |
| Responses by chat | Saved responses per chat. |
| Chat activity | Chats with a saved message in the preceding five minutes. |
| Chats | Saved and captured totals, responses, errors and availability for each chat or subtask. |
| Captured tokens · partial | Capture state, accumulated captured tokens, estimated tokens in range, capture start and generations without token counts. |

## Read the Values

**Saved history** covers each selected chat's recorded history. **Captured tokens**
accumulate from when capture starts. Read the [captured-token scope](../reference/retention-and-limits.md#token-capture)
for the meaning of **partial**. Compare the series independently; their histories
can overlap. Charts begin with the first Prometheus scrape. **K**, **Mil** and
**Bil** mean thousand, million and billion.

| Token counts | Meaning |
| --- | --- |
| Recorded | Valid saved input and output counts, including zero. |
| Not recorded by Bob | Bob omitted a saved count; responses and tool activity remain available. |
| Unavailable | Saved usage is invalid or parent/subtask reconciliation failed. |
| Unavailable / stale | Collection failed or its last successful scan is at least 90 seconds old. |

Missing saved cells show **Not recorded**; missing capture cells show **No captured count**.
An explicit recorded zero shows **0**. Overview's **Chat data coverage** describes
the database read. [Collector Health](../reference/collector-health.md#ibm-bob)
explains its states. Tables retain the last values during collection failure;
the affected charts leave gaps.

## Captured Tokens

Enable [Token Capture](../optional/bob-token-capture.md) to fill these values.

| Capture | Meaning |
| --- | --- |
| Capture disabled | The optional journal reader is off. |
| No activity | No saved message or captured generation is known for the chat. |
| Recorded zero · partial | Valid captured input and output are both zero. |
| Missing counts | Activity or a generation is known, but no captured total is available. |
| Partial capture | Captured counts are available. |
| Capture unavailable / stale | Journal access failed or the last successful check is at least 90 seconds old. |

**Estimated tokens in range** uses Prometheus scrape times and interpolation;
it can be fractional and needs enough samples. **Capture started** marks the
beginning of the current captured totals. **Generations without token counts**
counts observed generations with missing usage. Capture totals remain separate
from saved totals.
