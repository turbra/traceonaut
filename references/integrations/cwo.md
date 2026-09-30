---
slug: /integrations/cwo
title: CWO Integration
description: Collect CWO session usage, agent outcomes, workflow activity and contractor reviews.
---

# CWO Integration

[Complex Work Orchestration](https://github.com/gprocunier/complex-work-orchestration) is a Codex skill for coordinating agents and tracking work across sessions. Traceonaut reads the session files and reports that this work produces. It does not launch agents.

Start with **Collect CWO Sessions** for work performed using the CWO skill. The other sources add detail when their files exist.

| Data | Collector option | Source |
| --- | --- | --- |
| CWO sessions and subagents: tokens, latest model/effort, completed/failed turns, recorded turn time and helper commands | `--cwo-sessions` | Existing Codex session files. |
| Workflow events | `--cwo-audit-dir` | Existing CWO audit logs. |
| External contractor reviews | `--cwo-review-dir` | Paired launch receipts and result files. |

Each subagent has its own session measurements. A session that handles several assignments has combined totals: splitting them by assignment requires records that identify those boundaries. Recorded turn time measures reported turns, while review duration measures a contractor review. Neither proves the elapsed time or success of a larger assignment.

## Collect CWO Sessions

Append this flag to your [session collector command](../getting-started.mdx), then restart the collector:

```text
--cwo-sessions
```

This covers the **whole configured Codex profile**, across projects. It uses records already written by Codex:

- A structured CWO skill block associates its owning session.
- A supported CWO helper invocation associates its owning session.
- A native subagent created after its parent's CWO association inherits that association.

Import [CWO Overview](../dashboards/cwo.md). Its main overview shows sessions and agents active in the selected range, their recorded session usage, and CWO helper command counts. Initial history scanning runs in bounded passes; the dashboard shows pending files and source gaps.

**Association is session context.** Token totals cover whole sessions, including earlier history, rather than CWO-only cost. Plain mentions of CWO in conversation, reading a guide, or generic agent activity do not establish association. [Data Sources and Privacy](../reference/data-sources-and-privacy.md#cwo-session-association) describes the supported signals.

Collection uses the [supported session record formats](../reference/data-sources-and-privacy.md) with your existing Codex installation.

## Collect Workflow Activity

Append this option to your [session collector command](../getting-started.mdx):

```text
--cwo-audit-dir /absolute/path/to/cwo-project-state
```

Use the stable parent directory where your workflow writes its sprint logs, including a private local-state directory when that is the configured location. Repeat `--cwo-audit-dir` for each project's audit directory. Traceonaut finds `audit.jsonl` and `*-audit.jsonl` files in that directory and its subdirectories, including new sprint logs created later. For a different filename, use `--cwo-audit-file /absolute/path/to/workflow.jsonl`. Repeat it for additional files.

Choose the directories or files that your CWO workflow writes. CWO's `CWO_AUDIT_FILE` setting can select a custom location. Traceonaut reads existing files; enabling collection leaves your CWO launch and review workflow unchanged.

The collector exposes `cwo_audit_*` metrics on the **same authenticated endpoint** that Prometheus already scrapes. Import [CWO Overview](../dashboards/cwo.md) to see its **Workflow activity** section.

| Recorded event | Dashboard meaning |
| --- | --- |
| `packet_built` | A contractor packet was prepared. |
| `dispatch_prepared` | A dispatch was prepared for execution. |
| `return_evaluated` | A returned review was evaluated. Reevaluations count separately. |
| Review CLI and native-pool audit events | Recorded activity grouped by event type. |

These are workflow events, not completed-job or token totals. The existing audit format supplies an event type, timestamp and content hash. Formats without those fields appear as skipped records. Unknown event types are grouped as **Other audit events**.

Traceonaut verifies each record's content hash and deduplicates copies. It exports the newest **2,000 events within 30 days**, using their original timestamps. See [Retention and Limits](../reference/retention-and-limits.md#cwo-workflow-audits) for scan bounds and historical views.

Use `--once` with the same options for a first-run check. Its `cwo_audit` summary reports source availability, coverage and exported event count. A **complete** status means every selected audit file was read successfully within the limits. This measures those files, not all CWO usage. Incomplete reads make displayed counts lower bounds.

<a id="collect-cli-review-results"></a>

## Collect External Contractor Reviews

For workflows that retain paired launch receipts and Claude CLI JSON results, append:

```text
--cwo-review-dir /absolute/path/to/cwo-project-state
```

This opt-in reader discovers the following existing layout in that directory and its subdirectories:

```text
sprint/
  audit.jsonl
  reviewer-launch-receipt.json
  reviewer-response.raw.json
```

The prefix can vary. A launch receipt must contain `dispatch_id`, `packet_sha256`, a timezone-qualified `started_at`, and `requested_model`; `effort` is optional. The same directory's hash-valid audit must contain a matching `dispatch_prepared` event before the launch. The paired result must be a Claude CLI `type: result` object with `uuid`, `session_id`, and boolean `is_error`.

**External contractor reviews** shows each collected review attempt's outcome, requested and reported models, requested effort, token usage and duration. Attempts appear when their launch dates fall inside the selected range. Input includes uncached input, cache creation and cache reads. Thinking tokens are already included in output. Missing usage remains missing; a failed attempt with reported zero usage remains zero.

Copies of the same result count once. Conflicting copies are omitted and reported as skipped records. Repeated audit evaluations do not add token usage. These results stay separate from observed Codex jobs and whole-session token totals.

### Link Reviews to Sessions

Enable both `--cwo-sessions` and `--cwo-review-dir` to add **Source session** and **Attribution** to the review table. No additional launch step is required. Traceonaut reads existing session history and links a review to the session that ran its launch, including a subagent when that subagent was the launcher. Click the source name to open All Sessions for that session.

| Attribution | Meaning |
| --- | --- |
| Linked | Recorded launch and result evidence identifies one source session. |
| Unlinked | No supported matching launch was found, or session association is disabled. |
| Pending | Source scanning is incomplete or a relevant source could not be read. |
| Ambiguous | Evidence points to multiple sessions or cannot separate repeated results. |

All collected reviews remain visible, including failed attempts and retries. **Requested effort** comes from the launch receipt; the result reports the model, not an attestation of effort. Review usage stays separate from Codex session usage.

Supported evidence includes completed direct Claude commands with matching result identities, and paired-result `runner.py` launches whose recorded output matches the receipt's prompt hash, model, effort and result metadata. Interactive account-switched launches also require a recorded stdin command tied to the same process. Preparing a dispatch, reading a receipt, or mentioning a review does not establish a link. Other wrappers remain Unlinked until a reader supports their evidence.

The first scan backfills exported session histories in bounded passes. Subsequent scans resume from private cursors. `--once` reports `cwo_reviews.provenance` readiness and attribution counts. Source commands are inspected without execution; names and command text stay out of metric labels.

Choose roots that contain this layout. Other result formats need a supported reader; the collector does not infer review usage from prose or filenames alone. `--once` reports `cwo_reviews` source health, pending results and exported reviews. See [Retention and Limits](../reference/retention-and-limits.md#cwo-cli-review-results).

## Existing Controller Metrics

This advanced interface serves integrations that already write task records to Traceonaut's `observability.sqlite3` database. Those records remain available as `cwo_dispatch_*` and `cwo_cycle_*` metrics and through completed-dispatch export. CWO Overview uses the file sources above; it has no dependency on this interface.

The source-checkout launcher `run_observed_codex.py` is **deprecated** and excluded from release bundles. Its fixed legacy protocol is not maintained for current Codex versions. Do not downgrade Codex or launch extra model jobs to populate a dashboard. Existing ledgers remain readable.

### Use an Existing Ledger

The simplest option is to append `--state-dir /absolute/path/to/private-ledger` to the session collector command. This exposes dispatch metrics on the existing endpoint.

For a separate endpoint, from the checkout root:

```bash
python3 scripts/export_dispatch_observability.py \
  --state-dir /absolute/path/to/private-ledger \
  --credential-file /absolute/path/to/private/dispatch.token \
  --host 127.0.0.1 --port 9465
```

Set `--port 9465` explicitly: the CLI's compatible default is `9464`, also used by the session collector. Enable the commented `traceonaut-dispatches` job in the [scrape example](../../examples/observability/prometheus-scrape.yaml) only for this separate endpoint.

`--once` prints one metrics snapshot and exits. `--capacity-report` prints local ledger and series counts. Both read committed state without starting jobs.

The standalone endpoint is loopback-only. Use the shared session endpoint for the session collector's supported remote-scrape layout.

### Connect a Controller

Build the observation components:

```bash
TRACEONAUT_RELEASES_DIR="$HOME/.local/share/traceonaut/releases"
TRACEONAUT_DISPATCH_RELEASE="$(python3 scripts/build_release.py --component dispatch --output-dir "$TRACEONAUT_RELEASES_DIR")"
export PYTHONPATH="$TRACEONAUT_DISPATCH_RELEASE/scripts${PYTHONPATH:+:$PYTHONPATH}"
```

This is an embedding interface for controller maintainers, not a switch available in every CWO installation. Confirm that your controller supports the observation hook before using `--observability-config`. The configuration is an owned `0600` JSON file at an absolute path:

| Field | Content |
| --- | --- |
| `state_dir` | Absolute private ledger directory, separate from the checkout and Codex profile. |
| `registration` | [Registration schema](../../schemas/supervisor-project-registration-v1.schema.json) fields without `record_type`; matching process UID and `enabled` state. |
| `executor_vocabulary`, `model_vocabulary`, `effort_vocabulary` | Nonempty allowed label-value lists. |
| `source_compatibility_sha256` | Reference to compatibility evidence for the app-server protocol. |
| `metrics` | Numeric loopback `host`, integer `port`, absolute `credential_file`. |
| `prometheus` (optional) | Loopback `url`, exact scrape `job` and `instance`, optional protected `credential_file`. |

Use protected directories and preserve the ledger key. Traceonaut supplies the observer; CWO owns the controller. Existing ordinary sessions do not become dispatches by enabling this hook.

## View and Export

Import [CWO Overview](../dashboards/cwo.md), or use [Completed Dispatch Export](terminal-export.md). [Limits and Internals](../reference/limits-and-internals.md#cwo-embedding-and-recovery) covers embedding, recovery and final-sample confirmation.
