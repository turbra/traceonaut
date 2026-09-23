# Session collection

The collector discovers everyday Codex conversations, subagents, internal
agents, and archived sessions in one local profile. It follows new records
without restarting Codex or launching work through a special runner.

Start with [Set up Traceonaut](deployment.md). This reference explains what the
collector reads, what the metrics mean, and what it cannot measure.

## Source and token accounting

The collector reads `sessions/`, `archived_sessions/`, and selected metadata
columns in the profile's `state_*.sqlite` index. It does not modify those files.
Remote sessions and other profiles need their own collection. Work without a
local session record is not visible through this source.

- `token_usage_record` supplies response identity and usage. Records must name
  their owning thread. Thread and response IDs are deduplicated in the private
  index; copied parent history and compaction checkpoints do not add child usage.
  Conflicting identity or usage makes that session's numeric response accounting
  unavailable.
- Token totals cover available response records. Older history may lack them, so
  totals can be lower bounds. This is not billing data or proof of complete
  upstream delivery.
- Legacy `token_count` and positive index counters are separate runtime snapshots.
  They can contain estimates and are never added to response usage. An index
  zero is unknown until a numeric source event establishes zero.
- Model and effort are the latest selected settings. A session can change
  models; accumulated tokens are not attributed to its latest model.
- Working means an observed open turn with activity in the last two minutes.
  An older open turn is **No recent signal**. A completed turn means **Waiting**,
  not that the session has permanently ended. Turn time sums supplied
  completed-turn durations; it is not model compute time or an ETA.

The time picker selects sessions by their latest observed activity at range end.
Token, response, and turn totals cover those sessions' recorded history, not
usage spent exclusively within the selected interval. Prometheus history starts
with the first scrape. Backfill does not create earlier Prometheus samples.

The parser accepts specific record shapes, not arbitrary Codex log text.
Unsupported or invalid records appear in collector diagnostics. After a Codex
upgrade, check collection health and missing values before relying on totals.

## Recorded command source

Commands come from `event_msg` / `item_completed` records with item type
`CommandExecution`. The parser requires matching session ownership, turn and
item identity, and a valid completion timestamp. It hashes turn and item
identities before persistence and discards command text, output, and free-form
errors.

A completed status with exit code zero is a recorded success; a failed status
with a nonzero exit is a recorded failure. Unsupported or inconsistent
combinations remain unknown. Expected nonzero exits are included, so these are
command outcomes, not semantic task-success rates. A valid duration is the
reported `secs + nanos / 1e9`, independent of whether the outcome is known.

Exact duplicates do not change totals. Conflicting records make coverage
unavailable. Command discovery has separate resumable cursors and does not
rewind session or token accounting.

The exporter retains the newest **512 command observations within seven days**
by default. Timestamp and duration metrics use the same retained set. The
exclusive **Command history starts after** boundary advances past omitted
events, including timestamp ties. A covered interval must start after that
boundary, have completed backfill, and have fresh collection without unresolved
gaps or conflicts.

Counts use completion time within the selected interval. A command that starts
earlier can contribute its whole duration if it finishes inside the interval.
Parallel command durations must not be summed as elapsed wall time. Missing
duration is not zero. Positive unknown or conflicting counts can remain visible
as retained lower bounds when coverage is incomplete; a synthetic zero requires
covered history.

## Recorded compaction source

Compactions come from `event_msg` / `item_completed` records with item type
`ContextCompaction`. Ownership, timestamp, and identities are validated before
hashes and event time are stored. Replay is deduplicated; conflicting records
make coverage unavailable.

The exporter retains the newest **64 observations within seven days**, with a
separate complete-after boundary and coverage state. These events do not identify
manual versus automatic compaction, recovered tokens, active context use,
remaining space, or overflow. Cumulative token usage cannot provide those values.

## Limit session exposition

Per-session metrics expire after **30 days without an observed session event**,
with at most **1,000 sessions** exposed. The most recent eligible sessions win
the cap; session ID breaks ties. Creation time is used when no session event has
been observed. Unknown activity is not expired by age but remains subject to the
cap. A session exactly at the cutoff stays exposed.

Append these options to the continuous collector command to choose a 90-day
window and a 2,000-session cap:

```text
--session-retention-seconds 7776000 --session-export-cap 2000
```

`--session-retention-seconds 0` disables age expiry. The cap accepts 1 through
100,000. This policy does not delete Codex files or indexed accounting. Resumed
sessions become eligible again with their recorded accounting intact. The
protected snapshot and name selectors still contain every indexed session.
Command and compaction detail keep their separate windows and caps.

All dashboards share these limits. Current tables and totals cover session
groups exported at the selected range end. Increasing a range ending now does
not restore expired sessions. Choose a past end time to see previously scraped
sessions, within Prometheus retention. Sessions never scraped have no historical
Prometheus samples.

Beta's **Session export retention** diagnostics show the window, cap, exported
count, age-expired count, and cap-omitted count.
`cwo_codex_collector_sessions` counts all indexed sessions.
`cwo_codex_collector_session_export_*` gauges describe the export policy.
These limits bound current exposition, not database size or cumulative
Prometheus storage growth.

## Inspect collector health

Beta's collector-wide health row shows scan age, average errors per second over
one hour, and cumulative skipped records by reason. Project and Work filters
do not affect it. Missing metrics stay **Unavailable**, not zero. Traceonaut
does not install alert rules or notification services.

`cwo_codex_collector_skipped_records_total{reason=...}` is persisted with the
main reader's cursor. It starts when tracking is installed; upgrading does not
rewind files to invent counts. Command/compaction backfill does not count those
exclusions again. Source replacement, replay, and retries can count encounters
again, so these are not unique bad-record counts.

| Reasons | Meaning |
| --- | --- |
| `untracked_prefix`, `untracked_item`, `untracked_event` | Expected exclusions from the collection allowlist. Prefix filtering occurs before JSON parsing and cannot detect every format change. |
| `foreign_session`, `pre_session_history` | Copied or forked history excluded from this session's accounting. |
| `unsupported_record_type`, `non_object_record`, `invalid_payload`, `invalid_metadata`, `missing_session`, `invalid_timestamp`, `invalid_identity`, `invalid_usage`, `invalid_record`, `oversized_record` | Unsupported or rejected inputs worth inspecting, especially after an upgrade. |

Reasons are a fixed vocabulary, never record contents, filenames, or IDs.
Errors remain separate; one record can increment both counters. Nonzero skipped
totals alone do not establish collection failure.

**Collection status** requires a readable source and a recent scan.
**Latest source event age** measures actual session activity separately. An idle
source can have a healthy collector. Check pending files and errors when data
is missing; a successful HTTP scrape alone does not establish source health.

## Privacy and capacity

Only explicit session names, structured agent names/paths, and project folder
names supply display labels. Otherwise the fallback uses project, session kind,
and creation time. Prompt-derived titles, previews, messages, tool results, and
reasoning are not copied into state, metrics, logs, or presentation. Display
names stay in protected presentation files, not Prometheus labels.

The scanner prioritizes changed files and commits byte offsets with accounting.
It retries partial appended lines and preserves deduplication across restarts,
file replacement, and archive moves. Each pass has a 256 MiB read budget and a
16 MiB per-file budget. Lines over 8 MiB are skipped and counted. Malformed or
oversized response-record candidates invalidate numeric usage for their session.

The private database has a 2 GiB admission limit checked before each bounded
pass. Leave extra disk space for filesystem overhead and one pass of growth.
The source inventory is bounded at 100,000 files. Capacity or source-access
failure makes collection unavailable without changing Codex work or deleting
history. Measure Prometheus storage separately for your session volume.

## Add the Stable dashboard

With the collector from [setup](deployment.md) running, generate a separate
import file from the checkout root. Use your data directory if different:

<!-- render-stable -->
```bash
export TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
python3 scripts/render_codex_sessions_dashboard.py \
  --template examples/observability/grafana-sessions-dashboard.json \
  --snapshot-file "$TRACEONAUT_DATA_DIR/sessions.json" \
  --output "$TRACEONAUT_DATA_DIR/stable.json"
```

Import `stable.json` into Grafana and select the same Prometheus datasource.
Re-render and re-import to refresh names, or use optional
[automatic updates](operations.md#automatic-dashboard-name-updates).

Stable uses UID `cwo-supervisor-observability-v1`; do not provision it together
with the optional dispatch template, which uses the same UID.

[Account allowance](codex-beta-dashboard.md#account-allowance) is optional and
account-wide. [CWO dispatch collection](cwo-integration.md) is a separate optional
source; ordinary session records do not establish CWO ownership.
