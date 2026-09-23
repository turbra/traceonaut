---
slug: /dashboards/beta
---

# Beta dashboard

Beta shows local session activity, command outcomes, recorded usage, and
collector health. Its UID is `cwo-codex-beta`. It uses the file-based collector
and native Grafana panels, with no extra plugins.

CWO app-server dispatches have a separate [CWO dashboard](cwo-dashboard.md).
They are not part of Beta's session counts.

For installation, follow [Set up Traceonaut](deployment.md#3-import-a-dashboard-into-grafana).

## Choose work and time

**Project** and **Work** select readable names. Select a work title in a table
or chart to focus it; **All work** clears Work while preserving Project and the
time range. The selectors are independent: changing Project does not clear
Work, so an incompatible pair can correctly show no results.

The time picker selects sessions by their latest observed event at range end.
**Last observed** is age relative to that end time, not necessarily now.
Select it to open details with an exact timestamp in the dashboard timezone.

**Compare original** opens Stable with the same filters. Provision the
[Stable dashboard](codex-all-sessions-observability.md#add-the-stable-dashboard)
if you want that comparison. Beta can be used alone.

## Read the values

| Value | Meaning |
| --- | --- |
| Recorded tokens and responses | Available recorded session history, not spending inside the selected interval or billing. Older records may be absent. |
| Cached input / reasoning output | Subsets of input / output. Do not add them to total tokens. A missing or zero denominator makes percentage shares unavailable. |
| Observed turn time | Sum of supplied completed-turn durations, not session elapsed time, model compute time, or an ETA. |
| Model / effort | Latest selected settings, not proof of each response's actual model. |
| Working | An observed open turn with recent activity. It does not prove provider computation. |
| Waiting | The latest observed turn ended. It does not mean blocked, unhealthy, or finished work. |
| No recent signal | An open turn without recent activity. |

**Observed session history** labels the history totals. Coverage distinguishes
recorded, partial, runtime-only, unknown, and conflicted sources. Missing token
records show **Not recorded**, not zero. Expand **Diagnostics and detailed
coverage** to inspect the source.

Ranked token and turn-time charts omit missing values but retain measured zero,
so they can show different work populations. The session-state ring requires
consistent recognized state for every selected session. **No sessions match this
selection.** is a confirmed empty population; **State data unavailable.** means
missing, stale, or incomplete state evidence.

## Collector health and retention

The **Collector health** row shows:

- Age of `cwo_codex_collector_scan_timestamp_seconds`.
- `rate(cwo_codex_collector_errors_total[1h])`, summed in errors per second.
- Cumulative `cwo_codex_collector_skipped_records_total` by reason.

These are collector-wide values at range end, unaffected by Project and Work.
Missing data shows **Unavailable**; an error rate needs at least two samples.
Skipped records include expected exclusions and are not an error count.
No alerts or notifications are enabled.

Session export defaults to **30 days of inactivity** and a **1,000-session cap**.
**Session export retention** in Diagnostics shows the actual policy and omitted
counts. Current totals cover exported sessions even for a long range ending now.
To see expired sessions, select a past end time for which Prometheus has samples.
Source files and indexed accounting remain intact.
See [retention and health](codex-all-sessions-observability.md#limit-session-exposition)
for configuration and counter meanings.

## Find command failures and compactions

**Commands recorded** and **Recorded failures** count command completions inside
the selected interval. The work table includes those counts and the longest
reported command duration. State and configured model/effort are separate
range-end observations.

Failures include expected nonzero exits, such as a search with no matches.
They do not detect silent semantic errors. Duration describes reported command
execution, including a command that started before the interval. Command text,
output, prompts, and free-form errors are not collected.

By default, command detail retains the newest **512 observations within seven
days**. **Command history starts after** gives the exclusive coverage boundary.
Choose a later range start if necessary. **Command range** distinguishes covered
history from incomplete or unavailable collection. Zero requires covered
history; known positive counts can be lower bounds when coverage is incomplete.
Expand diagnostics to find unknown or conflicting outcomes.

**Observed compactions** counts recorded `ContextCompaction` completions, with
the newest **64 observations within seven days** retained separately. It does not
identify the cause, recovered tokens, context occupancy, remaining capacity, or
overflow. See the [source reference](codex-all-sessions-observability.md#recorded-command-source)
for command and compaction accounting limits.

## Override display names

The renderer normally uses the collector's protected name snapshot. To add
Beta-only aliases in the [dashboard-rendering shell](deployment.md#3-import-a-dashboard-into-grafana),
set `TRACEONAUT_BETA_LABELS="$TRACEONAUT_DATA_DIR/beta-labels.json"` and
add `--labels-file "$TRACEONAUT_BETA_LABELS"` to the Beta renderer command.

Create that JSON file as its owner, mode `0600`, in the existing private `0700`
collector data directory:

```json
{
  "version": 1,
  "projects": {"project-key": "Example project"},
  "sessions": {"session-key": "Example work"}
}
```

Use IDs from the local snapshot as keys. Project aliases must be distinct.
Aliases change only Beta presentation, not session names, metric labels, or the
other dashboards. Keep rendered definitions access-controlled because they
contain local display names.

## Refresh or remove Beta

Metrics update without re-importing. To refresh names and selector choices,
rerun the [render command](deployment.md#3-import-a-dashboard-into-grafana)
and re-import the JSON with the same UID. For automatic name updates, use an
optional [renderer watcher and file provider](operations.md#automatic-dashboard-name-updates).

Invalid input or unsafe output causes the renderer to exit. Inspect its output;
do not delete the session database to fix display names.

To remove a manually imported Beta, delete UID `cwo-codex-beta` through Grafana.
For a provisioned copy, stop its watcher and follow the provider's removal policy.
Leave the collector and other dashboards intact.

## Account allowance

The compact allowance strip shows available earned resets, remaining shared
weekly allowance, the scheduled weekly reset and its countdown, and account-read
freshness. These values apply to the account signed into the selected collector
profile. Project and Work filters do not change them. Like the state overview,
they are observations **at the selected range end**. The countdown and age of
the last successful read are measured from that range end, including for
historical ranges. History begins when account collection is enabled.

The reset count comes directly from Codex's `availableCount`; the returned
credit-detail list can be capped and is not counted. Zero means no earned resets
are available. Null, failed reads and stale observations remain unavailable.
Weekly allowance uses exactly one reported 10080-minute shared Codex window,
regardless of whether it is primary or secondary. Remaining percentage is
100 minus the service's reported usage, floored at zero. It is not a token budget
or proof that the next request is permitted. A passed scheduled reset shows
**Awaiting update** rather than assuming allowance has been restored.

The account poll runs approximately once per minute. A valid response is
**Current** for 150 seconds; optional fields can still be unavailable. Failed
reads clear numerical values immediately; a stopped poller becomes **Stale**.
The existing five-second dashboard refresh does not make additional account API
calls. Collection is independent of local session activity. Account history
follows whichever account is signed into the configured profile at each read;
it does not retain account identities or aggregate different accounts. The
queries require a single account exporter and suppress ambiguous populations.

This is optional and makes upstream account reads; it is not file-based session
accounting. To enable it after the [first-use setup](deployment.md), use the
same selected profile with an existing Codex login. In another terminal at the
checkout root, set these paths to match your installation:

```bash
TRACEONAUT_DATA_DIR="$HOME/.local/share/traceonaut"
TRACEONAUT_SOURCE_HOME="$HOME/.codex"
TRACEONAUT_CODEX_BINARY="/absolute/path/to/codex"
TRACEONAUT_ACCOUNT_STATE="$TRACEONAUT_DATA_DIR/account-state"
install -d -m 700 "$TRACEONAUT_ACCOUNT_STATE"
python3 scripts/collect_codex_account.py \
  --codex-bin "$TRACEONAUT_CODEX_BINARY" \
  --codex-home "$TRACEONAUT_SOURCE_HOME" \
  --snapshot-file "$TRACEONAUT_ACCOUNT_STATE/allowance.json"
```

Use absolute paths. Restart the session collector with
`--account-snapshot-file "$TRACEONAUT_DATA_DIR/account-state/allowance.json"`
added to its command. Keep this file separate from the session snapshot,
session database and Codex source files. Run the account collector under the
host service manager with upstream network access; the exporter only reads its
numeric snapshot and can remain in the existing Prometheus network namespace.
Use the same code version for both collectors; [pinned releases](operations.md#pinned-releases-and-upgrades)
are available for services. Omitting the account-snapshot argument leaves session
metrics unchanged.

The native client sends only initialization and `account/rateLimits/read`;
it never starts work or redeems a reset. Account polling is bounded and its own
process group is terminated after each read. The private snapshot is atomically
replaced and contains only numeric allowances, timestamps and availability.
Credentials, account IDs, credit IDs and backend messages are never copied to
the snapshot or metric labels. The reader uses the Codex app-server interface;
it adds no listener or panel plugin. Failed or incompatible account reads remain unavailable.
