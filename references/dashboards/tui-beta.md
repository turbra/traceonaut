---
slug: /dashboards/tui-beta
title: Codex TUI · Beta
description: A six-section experimental dashboard using existing Traceonaut session metrics.
---

# Codex TUI · Beta

![Codex TUI beta with synthetic example data](../../assets/screenshots/codex-tui-beta.png)

*Example data. No personal sessions are shown.*

This experimental view uses the Codex terminal dashboard's two-column layout
with Traceonaut's existing session metrics. Import it alongside your current
dashboards. It has its own UID and needs no additional collector.

## Import the Dashboard

With the session collector running, run this from the checkout root:

```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_codex_sessions_dashboard.py \
  --template examples/observability/codex-tui-beta.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --output "$TRACEONAUT_DATA_DIR/codex-tui-beta.json"
```

In Grafana, open **Dashboards → New → Import**, upload `codex-tui-beta.json`,
and select your existing Prometheus datasource. The title is **Codex TUI · Beta**.

For the full-width view shown above, use Grafana's kiosk mode. The normal view
keeps Grafana's navigation and the same six panels.

## Use the Dashboard

Choose a **Project**, **Session** and time range. The default is 24 hours, with
a 30-second refresh.

| Section | Shows |
| --- | --- |
| Overview | Collection health, scan age, working/waiting sessions, subagents and recorded tokens. |
| Token usage | Recorded input and output token totals over time. |
| Responses by chat | The eight selected sessions with the most recorded model responses. |
| Commands | Recorded commands and failures in the selected interval, with coverage. |
| Session activity | Working and waiting sessions over time. |
| Chats | Session name, state, model/effort and recorded tokens. Click a name to open Work Overview. |

Token and response totals cover recorded session history for the selected
sessions. The time range selects activity; it does not turn those totals into
interval consumption. Changing export membership can lower a historical total.
Commands marked **Partial** are lower bounds, shown as **≥**. Missing values
appear as **—**. Compact tokens use **K** (thousand), **Mil** (million) and
**Bil** (billion).

The layout is TUI-inspired; plugin counts, skill counts, account lifetime
statistics and per-chat allowance percentages are outside this view. See
[Reading the Values](reading-values.md) for shared metric meanings and
[Automatic Name Updates](../operations/automatic-name-updates.md) to refresh
session titles.
