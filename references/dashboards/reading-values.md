---
slug: /dashboards/reading-values
title: Reading the Values
description: Understand session states, time ranges, usage totals and unavailable values.
---

# Reading the Values

**Work** means one Codex session, including a subagent session. Its readable title is a label for the session's stable ID.

## Time and Selection

The time picker has a start and an end. Session views select sessions whose latest recorded activity at that end falls inside the range. **Last seen** is the age of that activity at the selected range end, so it stays meaningful when viewing an earlier range.

Token, response and turn totals cover each selected session's recorded history. Command and compaction counts cover completions inside the selected interval. Expired sessions can be viewed at a [past end time](../reference/retention-and-limits.md).

## Names and Counts

| Term | Meaning |
| --- | --- |
| Chat / session / work | One Codex conversation and its stable session ID. Work Overview calls it Work; the TUI dashboard calls it a Chat. |
| Response | A recorded model response. It is not the same as a user message or a completed turn. |
| Turn | One run from a user request or continuation until Codex finishes or stops. A turn can produce several model responses. Aborted turns are included in the stopped / failed count. |
| Native agent / subagent | A separate Codex session started as an agent. It has its own session measurements. |
| Helper command | A supported CWO command invocation recorded in a Codex command item. Failed invocations are still counted. |
| Contractor review | A review attempt launched through a configured external review adapter. |
| Workflow event | An event recorded in configured CWO audit logs. It is profile-wide and does not follow Project or Session filters. |

## Values

| Value | Meaning |
| --- | --- |
| Token counts | Tokens reported by the available response records for the selected sessions. Total tokens combine input and output. Older sessions may lack records, so treat totals as minimums rather than a bill. |
| Cached input / reasoning output | Parts of input / output respectively. Total tokens already include them. |
| Observed turn time | Sum of reported completed-turn durations. Parallel agents can overlap. |
| Model / effort | The latest selected settings for that session. Historical usage can span other settings. |
| Working | A turn is open and has had activity within two minutes. |
| Waiting | The last turn finished and no new turn has started. |
| Stopped / failed | The latest turn stopped, failed, or was aborted. Aborted turns are included in this state and in stopped / failed turn counts. |
| No recent signal | A turn remains open but has had no recent activity. |

Token summaries and comparison bars use compact values: **K = thousand, Mil = million, Bil = billion, Tri = trillion**. For example, `1.4 Bil` means 1.4 billion tokens and `7.2 Mil` means 7.2 million tokens. The token panels link here from their ⓘ descriptions.

Detail tables keep exact, grouped counts, such as `1,400,000,000`.

## Missing Values

A **—** means the source cannot supply a usable value. A measured zero stays zero. Partial token totals include the available records; conflicting records make usage unavailable.

### Command Coverage

**Covered** means the source is fresh, the collector has finished reading available files, and the selected interval is inside retained command history. **Partial** means some records may be missing because files are pending, parsing found gaps, or export limits removed older records.

A headline such as **≥ 236 · Partial** means at least 236 commands were recorded. **≥ 0 · Partial** means none were recorded, with incomplete coverage. A plain **0** under Covered means no matching command was found in the covered interval. Missing or stale collection shows a dash.

Work-table command columns remain numeric and follow the same coverage shown in the top strip. **Unknown** counts completions without a settled outcome. Compactions use separate coverage, available in Diagnostics.

Commands count reported exit outcomes, including expected nonzero exits. Command durations include the entire command when its completion falls in the interval. Compactions count recorded events; they do not measure context capacity.

Names are current display metadata, including in historical views. See [Data Sources and Privacy](../reference/data-sources-and-privacy.md) for what is retained.
