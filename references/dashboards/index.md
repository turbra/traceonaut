---
slug: /dashboards
title: Choosing a Dashboard
description: Choose a dashboard for Codex, IBM Bob or CWO activity.
---

# Choosing a Dashboard

Start with **Work Overview** for Codex or **IBM Bob · Beta** for Bob.

| Dashboard | Use it for | Source |
| --- | --- | --- |
| [Work Overview](work-overview.md) | Daily session and subagent activity, usage and commands. | Codex session files |
| [All Sessions](all-sessions.md) | A compact, session-centered alternative. | Codex session files |
| [CWO Overview](cwo.md) | CWO sessions, agents, usage, completed turns, failed / aborted turns and workflow activity. | Codex records; optional workflow logs and custom review-adapter results |
| [Codex TUI · Beta](tui-beta.md) | A minimal Grafana version of the Codex CLI Usage screen in its Dashboard view. | Existing Codex session metrics |
| [IBM Bob · Beta](ibm-bob-beta.md) | Bob chats, recorded token usage and tool results in a compact layout. | Bob Shell local SQLite database |

Each view has a separate UID, so they can coexist. The [Dashboard Reference](../reference/dashboards.md) lists template and renderer paths.

See [Reading the Values](reading-values.md) for Codex or
[Read the Values](ibm-bob-beta.md#read-the-values) for Bob before comparing usage
across time ranges.
