---
slug: /reference/metrics
title: Metrics
description: Metric names, wire types, labels and meanings for all collection sources.
---

# Metrics

Metric names are stable public identifiers. `cwo_codex_` identifies Codex collection,
`traceonaut_bob_` identifies IBM Bob collection, and `cwo_audit_` identifies optional
CWO workflow audits. Dispatch families describe optional controller observation.

`untyped` means the endpoint omits a Prometheus TYPE declaration. Read those
absolute snapshots as gauges. Use `rate()` and `increase()` with the counters
listed below. Labels contain opaque identities and bounded categories; readable
names stay in rendered dashboards.

## Codex

### Session Collector

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_collector_scan_timestamp_seconds` | untyped | None | Last finished scan pass, Unix seconds. |
| `cwo_codex_collector_last_event_timestamp_seconds` | untyped | None | Latest observed source event, Unix seconds. |
| `cwo_codex_collector_sessions` | untyped | None | All indexed sessions, including expired exports. |
| `cwo_codex_collector_pending_files` | untyped | None | Files still awaiting scan work. |
| `cwo_codex_collector_source_available` | untyped | None | Source access: 1 available, 0 unavailable. |
| `cwo_codex_collector_errors_total` | counter | `reason` | Persisted collector error encounters. |
| `cwo_codex_collector_skipped_records_total` | counter | `reason` | Persisted skipped-record encounters by reason. |
| `cwo_codex_collector_session_export_retention_seconds` | gauge | None | Session inactivity window; 0 disables age expiry. |
| `cwo_codex_collector_session_export_cap` | gauge | None | Maximum exported session groups. |
| `cwo_codex_collector_session_export_exported_sessions` | gauge | None | Sessions selected for current export. |
| `cwo_codex_collector_session_export_expired_sessions` | gauge | None | Sessions excluded by age. |
| `cwo_codex_collector_session_export_cap_omitted_sessions` | gauge | None | Eligible sessions excluded by the cap. |
| `cwo_codex_collector_session_export_cap_truncated` | gauge | None | 1 when the cap excludes eligible sessions. |

### Command and Compaction Detail

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_command_telemetry_snapshot_timestamp_seconds` | untyped | None | Telemetry snapshot time, Unix seconds. |
| `cwo_codex_command_telemetry_complete_after_timestamp_seconds` | untyped | None | Exclusive start boundary for fully covered history. |
| `cwo_codex_command_telemetry_observations` | untyped | None | Retained observation count. |
| `cwo_codex_command_telemetry_retention_seconds` | untyped | None | Detail retention window. |
| `cwo_codex_command_telemetry_cap` | untyped | None | Maximum retained observations. |
| `cwo_codex_command_telemetry_cap_truncated` | untyped | None | 1 when the observation cap truncates history. |
| `cwo_codex_command_telemetry_pending_files` | untyped | None | Files still to inspect for this event kind. |
| `cwo_codex_command_telemetry_backfill_complete` | untyped | None | 1 after the initial event scan completes. |
| `cwo_codex_command_telemetry_conflicts` | untyped | None | Conflicting event observations. |
| `cwo_codex_command_telemetry_source_gaps` | untyped | None | Known source gaps. |
| `cwo_codex_command_telemetry_ready` | untyped | None | 1 when event telemetry is ready. |
| `cwo_codex_compaction_telemetry_snapshot_timestamp_seconds` | untyped | None | Telemetry snapshot time, Unix seconds. |
| `cwo_codex_compaction_telemetry_complete_after_timestamp_seconds` | untyped | None | Exclusive start boundary for fully covered history. |
| `cwo_codex_compaction_telemetry_observations` | untyped | None | Retained observation count. |
| `cwo_codex_compaction_telemetry_retention_seconds` | untyped | None | Detail retention window. |
| `cwo_codex_compaction_telemetry_cap` | untyped | None | Maximum retained observations. |
| `cwo_codex_compaction_telemetry_cap_truncated` | untyped | None | 1 when the observation cap truncates history. |
| `cwo_codex_compaction_telemetry_pending_files` | untyped | None | Files still to inspect for this event kind. |
| `cwo_codex_compaction_telemetry_backfill_complete` | untyped | None | 1 after the initial event scan completes. |
| `cwo_codex_compaction_telemetry_conflicts` | untyped | None | Conflicting event observations. |
| `cwo_codex_compaction_telemetry_source_gaps` | untyped | None | Known source gaps. |
| `cwo_codex_compaction_telemetry_ready` | untyped | None | 1 when event telemetry is ready. |
| `cwo_codex_command_event_timestamp_seconds` | untyped | `project_id`, `session_id`, `observation_id`, `outcome` | Command completion time, Unix seconds. |
| `cwo_codex_command_event_duration_seconds` | untyped | `project_id`, `session_id`, `observation_id`, `outcome` | Reported whole-command duration in seconds. |
| `cwo_codex_compaction_observation_timestamp_seconds` | untyped | `project_id`, `session_id`, `observation_id` | Compaction completion time, Unix seconds. |

### Sessions

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_session_info` | untyped | `project_id`, `session_id`, `kind`, `parent_id`, `model`, `effort` | Session identity and latest selected settings; value 1. |
| `cwo_codex_session_last_event_timestamp_seconds` | untyped | `project_id`, `session_id` | Latest observed session activity, Unix seconds. |
| `cwo_codex_session_state` | untyped | `project_id`, `session_id` | Session activity state code. |
| `cwo_codex_session_reported_tokens` | untyped | `project_id`, `session_id` | Separate legacy runtime token snapshot. |
| `cwo_codex_session_response_count` | untyped | `project_id`, `session_id` | Deduplicated recorded response count. |
| `cwo_codex_session_completed_turns` | untyped | `project_id`, `session_id` | Recorded completed turns. |
| `cwo_codex_session_failed_turns` | untyped | `project_id`, `session_id` | Recorded failed or aborted turns. |
| `cwo_codex_session_observed_turn_seconds` | untyped | `project_id`, `session_id` | Sum of reported turn durations. |
| `cwo_codex_session_usage_state` | untyped | `project_id`, `session_id` | Recorded-usage coverage code. |
| `cwo_codex_session_usage_tokens` | untyped | `project_id`, `session_id`, `token_kind` | Deduplicated recorded tokens by category. |

### Account Allowance

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_account_available` | gauge | None | 1 when the latest account read is available. |
| `cwo_codex_account_last_attempt_timestamp_seconds` | gauge | None | Most recent account-read attempt. |
| `cwo_codex_account_last_success_timestamp_seconds` | gauge | None | Most recent successful account read. |
| `cwo_codex_account_reset_credits_available` | gauge | None | Available earned reset credits. |
| `cwo_codex_account_window_used_percent` | gauge | `window` | Reported used allowance, percent. |
| `cwo_codex_account_window_minutes` | gauge | `window` | Reported account-window duration, minutes. |
| `cwo_codex_account_window_reset_timestamp_seconds` | gauge | `window` | Scheduled reset, Unix seconds. |

### CWO Dispatches

These metrics come from an existing controller database supplied through `--state-dir`. They remain available for custom integrations and queries; CWO Overview uses session, audit and review metrics instead.

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_dispatch_info` | gauge | `project_id`, `dispatch_id`, `agent_id`, `packet_ref`, `requested_model`, `requested_effort` | Dispatch/agent identity and requested settings; value 1. |
| `cwo_dispatch_configured_model_info` | gauge | `project_id`, `dispatch_id`, `configured_model` | Acknowledged model setting; value 1. |
| `cwo_dispatch_configured_effort_info` | gauge | `project_id`, `dispatch_id`, `configured_effort` | Acknowledged effort setting; value 1. |
| `cwo_dispatch_snapshot_revision` | gauge | `project_id`, `dispatch_id` | Published accounting snapshot revision. |
| `cwo_dispatch_state` | gauge | `project_id`, `dispatch_id` | Observed lifecycle state code. |
| `cwo_agent_state` | gauge | `project_id`, `agent_id` | Observed agent lifecycle state code. |
| `cwo_dispatch_completed_cycles_total` | counter | `project_id`, `dispatch_id` | Observed completed model responses. |
| `cwo_cycle_present` | gauge | `project_id`, `dispatch_id`, `cycle_ordinal` | Retained response identity; value 1. |
| `cwo_cycle_observed_timestamp_seconds` | gauge | `project_id`, `dispatch_id`, `cycle_ordinal` | Response observation time, Unix seconds. |
| `cwo_cycle_tokens` | gauge | `project_id`, `dispatch_id`, `cycle_ordinal`, `token_kind` | Reported tokens for one response, by category. |
| `cwo_dispatch_observed_tokens` | gauge | `project_id`, `dispatch_id`, `token_kind` | Reported tokens accumulated for a dispatch. |
| `cwo_cycle_token_state` | gauge | `project_id`, `dispatch_id`, `cycle_ordinal`, `token_kind` | Availability/provenance code for response token values. |
| `cwo_dispatch_token_state` | gauge | `project_id`, `dispatch_id`, `token_kind` | Availability/provenance code for aggregate tokens. |
| `cwo_dispatch_token_cycles` | gauge | `project_id`, `dispatch_id`, `token_kind` | Response contributions by token category. |
| `cwo_dispatch_elapsed_seconds` | gauge | `project_id`, `dispatch_id` | Elapsed time with qualified lifecycle timing. |
| `cwo_dispatch_declared_cycle_allowance` | gauge | `project_id`, `dispatch_id` | Declared response allowance. |
| `cwo_dispatch_declared_cycle_remaining` | gauge | `project_id`, `dispatch_id` | Remaining declared response allowance. |
| `cwo_dispatch_declared_cycle_overrun` | gauge | `project_id`, `dispatch_id` | Responses beyond the declared allowance. |
| `cwo_dispatch_declared_elapsed_allowance_seconds` | gauge | `project_id`, `dispatch_id` | Declared time allowance. |
| `cwo_dispatch_declared_elapsed_remaining_seconds` | gauge | `project_id`, `dispatch_id` | Remaining declared time allowance. |
| `cwo_dispatch_declared_elapsed_overrun_seconds` | gauge | `project_id`, `dispatch_id` | Time beyond the declared allowance. |
| `cwo_dispatch_enforced_tool_call_limit` | gauge | `project_id`, `dispatch_id` | Enforced tool-call limit. |
| `cwo_dispatch_enforced_runtime_limit_seconds` | gauge | `project_id`, `dispatch_id` | Enforced runtime limit. |
| `cwo_dispatch_field_state` | gauge | `project_id`, `dispatch_id`, `field` | Availability/provenance code by field. |
| `cwo_dispatch_retry_notices_total` | counter | `project_id`, `dispatch_id` | Observed retry notices. |
| `cwo_dispatch_turn_outcomes_total` | counter | `project_id`, `dispatch_id`, `outcome` | Observed turn outcomes by outcome label. |
| `cwo_dispatch_tool_events_total` | counter | `project_id`, `dispatch_id`, `category`, `lifecycle` | Observed tool events by category and lifecycle. |
| `cwo_telemetry_events_total` | counter | `project_id`, `disposition` | Accepted, dropped or otherwise classified telemetry events. |
| `cwo_telemetry_component_state` | gauge | `project_id`, `component`, `state` | Collection component/state indicator. |
| `cwo_telemetry_last_event_timestamp_seconds` | gauge | `project_id` | Latest telemetry event, Unix seconds. |
| `cwo_telemetry_queue_depth` | gauge | `project_id` | Pending telemetry queue size. |
| `cwo_telemetry_ledger_bytes` | gauge | `project_id` | Observed ledger storage size. |
| `cwo_telemetry_publication_state` | gauge | `project_id` | Stored-sample confirmation status. |
| `cwo_telemetry_publication_pending_dispatches` | gauge | `project_id` | Dispatches awaiting final-sample confirmation. |
| `cwo_dispatch_coverage_state` | gauge | `project_id`, `dispatch_id` | Known-gap/accounting-conflict coverage code. |

### State Codes

Session activity: `0` Unknown, `1` Working, `2` Waiting, `3` Stopped / failed, `4` No recent signal.
Session usage: `0` unknown, `1` recorded, `2` runtime-only, `3` conflicted, `4` partial.

CWO field/token state: `0` unavailable, `1` present, `2` runtime-normalized, `3` invalid, `4` partial, `5` unqualified, `6` clock gap, `7` overflow.
CWO coverage: `0` unknown, `1` observed with no known gap, `2` known gap, `3` accounting conflict.

See [Reading the Values](../dashboards/reading-values.md), [Collector Health](collector-health.md), and the [dispatch contract](../../scripts/traceonaut/observability_contract.py) for scope and remaining enumerations.

### CWO Workflow Audits

These optional gauges come from configured CWO audit JSONL, independently of the observed-dispatch ledger. Event timestamps describe when the source recorded an event. `skipped_records` and `source_errors` describe the latest scan.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_audit_source_available` | gauge | None | At least one configured audit file was read. |
| `cwo_audit_collection_complete` | gauge | None | All configured audit sources were read without gaps or export limits. |
| `cwo_audit_scan_timestamp_seconds` | gauge | None | Unix time of the latest completed audit scan attempt. |
| `cwo_audit_source_files` | gauge | None | Distinct audit files successfully read in the latest scan. |
| `cwo_audit_source_errors` | gauge | None | Source access failures in the latest scan. |
| `cwo_audit_limit_reached` | gauge | None | A discovery, byte or event export limit was reached. |
| `cwo_audit_exported_events` | gauge | None | Unique audit events currently exported within the retention window. |
| `cwo_audit_skipped_records` | gauge | `reason` | Records omitted in the latest scan, by bounded reason. |
| `cwo_audit_event_timestamp_seconds` | gauge | `event_id`, `event_type` | Source Unix timestamp of one unique CWO audit event. |

Skip reasons: `invalid_json`, `unsupported_record`, `invalid_hash`, `invalid_timestamp`, `future_timestamp`, `oversized_line`, `partial_line`. Unknown valid event types use `other`. The content hash is verified for consistency and deduplication.

Recognized event types in the `event_type` label:

| Event type | Meaning |
| --- | --- |
| `packet_built` | A contractor packet was prepared. |
| `dispatch_prepared` | A dispatch was prepared for execution. |
| `return_evaluated` | A returned review was evaluated; reevaluations are separate events. |
| `native_pool_rendered` | A native agent-group report was rendered. |
| `native_pool_status` | A native agent-group status was recorded. |
| `native_pool_interrupt_requested` | An interrupt request for a native agent group was recorded. |
| `native_pool_terminal` | A native agent group reached a recorded terminal state. |

Here, a native agent group is a set of Codex native subagents supervised together. These events describe the group-level report or state; they describe the supervised group as a whole.

Audit events record logged workflow activity.

### CWO-Associated Sessions

These optional gauges come from Codex rollout records with `--cwo-sessions`. The dashboard joins association with the existing session metrics.

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_cwo_scan_timestamp_seconds` | gauge | None | Latest CWO session source scan attempt, Unix seconds. |
| `cwo_codex_cwo_scan_ready` | gauge | None | Selected session sources scanned without pending files, access errors or retained gaps. |
| `cwo_codex_cwo_pending_files` | gauge | None | Selected rollout files still awaiting CWO association scanning. |
| `cwo_codex_cwo_source_errors` | gauge | None | CWO source access failures in the latest scan. |
| `cwo_codex_cwo_source_gaps` | gauge | None | Persisted malformed or oversized candidate records and command conflicts. |
| `cwo_codex_cwo_limit_reached` | gauge | None | Source-file or completed-command export cap reached. |
| `cwo_codex_session_cwo_association_timestamp_seconds` | gauge | `project_id`, `session_id`, `source` | First CWO association: structured skill block, direct helper command, or parent session; identifies associated sessions. |
| `cwo_codex_cwo_command_timestamp_seconds` | gauge | `project_id`, `session_id`, `observation_id`, `tool`, `outcome` | Source completion time of a supported CWO helper invocation. |
| `cwo_codex_cwo_command_duration_seconds` | gauge | `project_id`, `session_id`, `observation_id`, `tool`, `outcome` | Reported duration when the CWO helper is the sole command. |

Association sources: `skill_block`, `tool_execution`, `parent_session`. Command outcomes: `completed`, `failed`, `unknown`. Supported helper names: `build_contractor_packet`, `close_bead_with_summary`, `coach_prompt`, `dispatch_work`, `evaluate_return`, `normalize_contractor_return`, `render_execution_status_report`, `route_work`, `run_checked_command`, `supervise_native_pool`, `supervise_native_worker`, `validate_operator_handoff`, `validate_run_readiness_plan`.

### CLI Review Results

Optional review artifact collection uses gauges. Review usage is separate from Codex session and observed-dispatch accounting.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_review_source_available` | gauge | None | At least one configured review launch or provenance record was read. |
| `cwo_review_collection_complete` | gauge | None | Enabled review sources read without errors, missing results or limits. |
| `cwo_review_scan_timestamp_seconds` | gauge | None | Unix time of the latest CLI review scan attempt. |
| `cwo_review_source_files` | gauge | None | Review launch and provenance records read in the latest scan. |
| `cwo_review_source_errors` | gauge | None | Review artifact access failures in the latest scan. |
| `cwo_review_pending_results` | gauge | None | Recorded launches without a saved final result. |
| `cwo_review_limit_reached` | gauge | None | Review artifact discovery, byte or export cap reached. |
| `cwo_review_skipped_records` | gauge | `reason` | Review artifacts omitted in the latest scan by bounded reason. |
| `cwo_review_started_timestamp_seconds` | gauge | `review_id`, `outcome`, `requested_model`, `reported_model`, `effort` | Recorded launch time of an external review invocation. |
| `cwo_review_tokens` | gauge | `review_id`, `outcome`, `requested_model`, `reported_model`, `effort`, `kind` | CLI top-level usage by kind; thinking is a subset of output. Input excludes cache creation and reads. |
| `cwo_review_duration_seconds` | gauge | `review_id`, `outcome`, `requested_model`, `reported_model`, `effort` | CLI-reported result duration. |
| `cwo_review_session_info` | gauge | `review_id`, `project_id`, `session_id` | Value 1 identifies the launching Codex session for a Linked review. |
| `cwo_review_attribution_state` | gauge | `review_id`, `state` | Value 1 for the review's current state: `linked`, `unlinked`, `pending` or `ambiguous`. |
| `cwo_review_discovered_launches` | gauge | None | Review launches found in retained CWO session commands; excludes model checks. |
| `cwo_review_discovery_gaps` | gauge | `reason` | Incomplete discovered evidence, grouped by bounded reason. |
| `cwo_review_record_state` | gauge | `review_id`, `record_state` | Complete result or reason its evidence is unavailable. |
| `cwo_review_evaluation_info` | gauge | `review_id`, `verdict` | Recorded evaluator verdict; `accept_pending_peer` retains an outstanding peer-review hold. |
| `cwo_review_snapshot_timestamp_seconds` | gauge | `review_id`, `outcome`, `requested_model`, `reported_model`, `effort`, `state`, `record_state`, `verdict`, `project_id`, `session_id`, `input_available`, `output_available`, `duration_available` | Scan time and current record metadata used to reconcile later-collected historical reviews. |

Discovery gap reasons are `missing_result`, `conflicting_result`, `invalid_record`, `oversized_output`, `changing_output`, `unreadable_output`, `reused_output` and `unsupported_launch`. They describe saved evidence, not whether a review was accepted. CLI completion, evaluator verdict and implementation outcome are separate facts.

## IBM Bob

### Saved history

Bob gauges describe currently saved records. Chat identity uses hashed task IDs
in `session_id`; projects use hashed IDs in `project_id`. Parent token values
exclude completed subtask costs when the saved values support that calculation.
Uncertain parent values are unavailable. Cache fields are input breakdowns;
`total` is input plus output.

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `traceonaut_bob_collector_source_available` | gauge | None | One when the source was readable on the latest scan. |
| `traceonaut_bob_collector_scan_timestamp_seconds` | gauge | None | Latest scan attempt, Unix seconds. |
| `traceonaut_bob_collector_last_success_timestamp_seconds` | gauge | None | Latest successful collection check, Unix seconds; includes incremental and unchanged-source checks. See [Bob data freshness](retention-and-limits.md#ibm-bob). |
| `traceonaut_bob_collector_collection_complete` | gauge | None | One when collection finished without source errors, skips, invalid token values, unsafe subtask reconciliation or unknown tool outcomes. Omitted token fields alone leave this at one. |
| `traceonaut_bob_collector_pending` | gauge | None | One while a bounded source read continues over later scans. |
| `traceonaut_bob_collector_limit_reached` | gauge | None | One when a scan or storage limit prevents completion. |
| `traceonaut_bob_collector_source_errors` | gauge | None | Source failures in the latest scan. |
| `traceonaut_bob_collector_indexed_sessions` | gauge | None | Chats in the last completed source read. |
| `traceonaut_bob_collector_expired_sessions` | gauge | None | Chats excluded by inactivity retention. |
| `traceonaut_bob_collector_cap_omitted_sessions` | gauge | None | Eligible chats omitted by the export cap. |
| `traceonaut_bob_collector_retention_seconds` | gauge | None | Per-source export inactivity window; zero disables expiry. |
| `traceonaut_bob_collector_export_cap` | gauge | None | Maximum exported Bob chats. |
| `traceonaut_bob_collector_skipped_records` | gauge | `reason` | Skips in the latest completed source read. |
| `traceonaut_bob_session_info` | gauge | `project_id`, `session_id`, `kind` | Bob chat identity; value 1. |
| `traceonaut_bob_session_usage_tokens` | gauge | `project_id`, `session_id`, `token_kind` | Recorded tokens by input, output, cached_input, cache_write_input or total. |
| `traceonaut_bob_session_token_status` | gauge | `project_id`, `session_id`, `token_kind` | Availability of input, output and total: 0 recorded, 1 not recorded by Bob, 2 invalid saved value, 3 reconciliation unavailable. |
| `traceonaut_bob_session_last_event_timestamp_seconds` | gauge | `project_id`, `session_id` | Latest saved message time, Unix seconds. |
| `traceonaut_bob_session_response_count` | gauge | `project_id`, `session_id` | Saved assistant responses, excluding local UI messages. |
| `traceonaut_bob_session_tool_result_count` | gauge | `project_id`, `session_id` | Saved tool results, including unknown outcomes. |
| `traceonaut_bob_session_tool_error_count` | gauge | `project_id`, `session_id` | Saved tool results explicitly marked as errors. |
| `traceonaut_bob_session_tool_unknown_count` | gauge | `project_id`, `session_id` | Saved tool results with unknown outcomes. |
| `traceonaut_bob_session_tool_duration_seconds` | gauge | `project_id`, `session_id` | Sum of recorded tool durations in seconds. |
| `traceonaut_bob_session_timed_tool_count` | gauge | `project_id`, `session_id` | Tool results with recorded durations. |
| `traceonaut_bob_session_partial` | gauge | `project_id`, `session_id` | One when chat usage or tool outcomes have missing or inconsistent fields. |

Chat kinds: `normal`, `subtask`, `subagent`. Skip reasons: `invalid_record`,
`invalid_json`, `invalid_number`, `invalid_timestamp`, `unsupported_version`,
`unsupported_kind`, `oversized_record`, `unknown_tool_outcome`.

Absent or null token fields have status 1; invalid fields have status 2; withheld
parent values have status 3. A total requires valid input and output. Optional
cache fields and each input/output value remain independently usable. Older
collectors without status metrics display **Unavailable**. `session_partial`
includes missing token fields separately from collection coverage.

Known zero values are exported; missing usage and durations are omitted. A source
failure retains the last numeric cache with availability 0 and the previous
last-success timestamp. See the [IBM Bob source](../sources/ibm-bob.md).

### Optional Generation Capture

Enable the reader with `--bob-otel-journal-dir`. Captured values are counters;
saved history uses gauges. See [token scope](retention-and-limits.md#token-capture)
and [setup](../optional/bob-token-capture.md).

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `traceonaut_bob_capture_enabled` | gauge | None | One when the optional journal reader is enabled. |
| `traceonaut_bob_capture_source_available` | gauge | None | One when the latest journal access succeeded. |
| `traceonaut_bob_capture_last_success_timestamp_seconds` | gauge | None | Latest successful journal check, Unix seconds. |
| `traceonaut_bob_capture_epoch_timestamp_seconds` | gauge | None | Start of the durable capture ledger epoch, Unix seconds. |
| `traceonaut_bob_capture_backlog_bytes` | gauge | None | Unread bytes, including incomplete trailing records. |
| `traceonaut_bob_capture_pending_joins` | gauge | None | Events withheld pending a known chat identity. |
| `traceonaut_bob_capture_scope_partial` | gauge | None | One for the enabled generation-token scope. |
| `traceonaut_bob_capture_errors` | gauge | None | Latest journal read failed independently of database collection. |
| `traceonaut_bob_capture_loss_total` | counter | None | Detected lost, rejected, conflicting or expired records. |
| `traceonaut_bob_session_captured_tokens_total` | counter | `project_id`, `session_id`, `token_kind`, `event_kind` | Accepted generation tokens within the epoch. |
| `traceonaut_bob_session_capture_status` | gauge | `project_id`, `session_id` | 0 disabled, 1 no activity, 2 recorded zero with partial scope, 3 missing counts, 4 partial capture, 5 unavailable/stale. |
| `traceonaut_bob_session_capture_missing_events_total` | counter | `project_id`, `session_id` | Joined generation records without valid counts. |

Capture token kinds are `input`, `output`, `total`, `cached_input`,
`cache_write_input` and `reasoning`; `event_kind` is `generation`. An optional
detail total is omitted if any counted event lacks that field. Export selection
uses the same per-source retention and cap as saved chats. Metric labels exclude
trace/span IDs, producer/model names, titles, users and paths.

See [capture storage and replay](limits-and-internals.md#token-capture) and
[dashboard values](../dashboards/ibm-bob-beta.md#captured-tokens).
