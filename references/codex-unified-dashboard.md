# Unified dashboard

Unified combines session and agent inventory, activity, command outcomes, and
recorded usage. It uses the same collector and Prometheus data as Beta, with a
separate UID: `cwo-codex-unified`. It needs no additional collector or Grafana
plugin.

## Read the overview

**Project** and **Work** select sessions by stable IDs. A session matches when
its latest observed event at range end falls within the selected interval.
The selectors are independent; incompatible selections can match no sessions.
**All work** clears Work without clearing Project.

The dashboard groups information into:

- **Overview:** matching work, range-end states, collection and coverage.
  Subagents are a subset of sessions, not an extra count to add to the total.
- **Account allowance:** optional account-wide values, independent of Project
  and Work. See [account setup](codex-beta-dashboard.md#account-allowance).
- **Sessions and activity:** a working-state trend, state distribution, interval
  command counts, and the work/agent inventory.
- **Usage:** recorded token history, its composition, and work comparisons.
- **Time and execution:** accumulated turn time and recorded turn counts.
- Collapsed details for session metadata, commands, compactions, and coverage.

Working means an observed open turn with recent activity, not proven provider
computation. Waiting means the latest observed turn ended, not that the work
is blocked or complete. Empty selections and unavailable state data have
different explanations. Missing observations are not filled with zero.

The activity chart samples at most 180 points; brief transitions can fall
between samples in a broad range. Missing collection leaves gaps. The inventory
and ranked lists can scroll. Select a work title to focus it, or its age to see
the exact last-observed timestamp. Ages are relative to range end.

## Understand names and totals

Unified uses collected names, not Beta's optional aliases. Name precedence is
explicit session `name`, the final `agent_path` component, then a
project/kind/start-time fallback. Duplicate names are disambiguated.
Prompt-derived titles, previews, and first messages are excluded.

Names are current presentation metadata even when you choose a historical
range. IDs, not names, define joins and session identity. Model and effort are
recorded selected settings, not proof of each response's actual model.

Token, response, and turn totals cover available recorded session history.
They are not selected-interval consumption. Cached input is part of input;
reasoning output is part of output. Runtime-reported token snapshots remain
separate from response usage. Partial or conflicting data is not presented as
complete.

Commands and command failures count completions inside the interval. Failures
include expected nonzero exits and are distinct from failed turns. Incomplete
coverage can preserve a positive lower bound while withholding an unsupported
zero. Observed turn time can overlap across agents and does not measure how
long the user waited. Compactions do not measure context utilization or cause.

[Session export limits](codex-all-sessions-observability.md#limit-session-exposition)
apply to Unified too. Current totals exclude sessions no longer exported.
Diagnostic command/compaction tables can retain event evidence after session
metadata is unavailable; those events do not create extra inventory rows.

## Add Unified

With the collector from [setup](deployment.md) running, generate Unified's JSON
from the checkout root. Use your data directory if different:

<!-- render-unified -->
```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_codex_unified_dashboard.py \
  --template examples/observability/grafana-codex-unified-dashboard.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --output "$TRACEONAUT_DATA_DIR/unified.json"
```

Import `unified.json` into Grafana and select the same Prometheus datasource.
Leave Beta and Stable's definitions intact; Unified has its own UID.
Re-render and re-import to refresh names, or use optional
[automatic updates](operations.md#automatic-dashboard-name-updates) with this
renderer and template.

The five-second dashboard refresh does not change the optional account reader's
polling cadence.
