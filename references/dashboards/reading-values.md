---
slug: /dashboards/reading-values
title: Reading the Values
description: Understand session states, time ranges, usage totals and unavailable values.
---

# Reading the Values

**Work** means one Codex session, including a subagent session. Its readable title is a label for the session's stable ID.

## Time and Selection

The time picker has a start and an end. Session views select sessions whose latest recorded activity at that end falls inside the range. **Last observed** is the age of that activity at the selected end time.

Token, response and turn totals cover each selected session's recorded history. Command and compaction counts cover completions inside the selected interval. Expired sessions can be viewed at a [past end time](../reference/retention-and-limits.md).

## Values

| Value | Meaning |
| --- | --- |
| Token counts | Whole numbers with digit grouping, such as `1,400,000,000`. Older sessions may lack records, so treat totals as minimums rather than a bill. |
| Cached input / reasoning output | Parts of input / output respectively. Total tokens already include them. |
| Observed turn time | Sum of reported completed-turn durations. Parallel agents can overlap. |
| Model / effort | The latest selected settings for that session. Historical usage can span other settings. |
| Working | A turn is open and has had activity within two minutes. |
| Waiting | The last turn finished and no new turn has started. |
| Stopped / failed | The latest turn stopped or failed. |
| No recent signal | A turn remains open but has had no recent activity. |

## Missing Values

**Not recorded** and **Unavailable** mean the source cannot supply a usable value. A measured zero stays zero. Partial token totals include the available records; conflicting records make usage unavailable.

Commands count reported exit outcomes, including expected nonzero exits. Command durations include the entire command when its completion falls in the interval. Compactions count recorded events; they do not measure context capacity.

Names are current display metadata, including in historical views. See [Data Sources and Privacy](../reference/data-sources-and-privacy.md) for what is retained.
