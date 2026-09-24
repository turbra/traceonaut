---
slug: /reference/metrics
title: Metrics
description: Metric names, wire types, labels and meanings for all collection sources.
---

# Metrics

Metric names are stable public identifiers. The `cwo_codex_` prefix belongs to session/account collection and optional CWO session association; `cwo_audit_` belongs to optional workflow audit collection. The remaining families describe optional dispatch observation.

`untyped` below means the endpoint omits a Prometheus TYPE declaration. Those values are absolute snapshots, read like gauges. Apply `rate()` only to the counters identified here.

Labels use opaque identities and bounded categories. Display names stay in rendered dashboards.

## Session Collector

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_collector_scan_timestamp_seconds` | untyped | None | Last completed scan, Unix seconds. |
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

## Command and Compaction Detail

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

## Sessions

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_session_info` | untyped | `project_id`, `session_id`, `kind`, `parent_id`, `model`, `effort` | Session identity and latest selected settings; value 1. |
| `cwo_codex_session_last_event_timestamp_seconds` | untyped | `project_id`, `session_id` | Latest observed session activity, Unix seconds. |
| `cwo_codex_session_state` | untyped | `project_id`, `session_id` | Session activity state code. |
| `cwo_codex_session_reported_tokens` | untyped | `project_id`, `session_id` | Separate legacy runtime token snapshot. |
| `cwo_codex_session_response_count` | untyped | `project_id`, `session_id` | Deduplicated recorded response count. |
| `cwo_codex_session_completed_turns` | untyped | `project_id`, `session_id` | Recorded completed turns. |
| `cwo_codex_session_failed_turns` | untyped | `project_id`, `session_id` | Recorded failed turns. |
| `cwo_codex_session_observed_turn_seconds` | untyped | `project_id`, `session_id` | Sum of reported completed-turn durations. |
| `cwo_codex_session_usage_state` | untyped | `project_id`, `session_id` | Recorded-usage coverage code. |
| `cwo_codex_session_usage_tokens` | untyped | `project_id`, `session_id`, `token_kind` | Deduplicated recorded tokens by category. |

## Account Allowance

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_account_available` | gauge | None | 1 when the latest account read is available. |
| `cwo_codex_account_last_attempt_timestamp_seconds` | gauge | None | Most recent account-read attempt. |
| `cwo_codex_account_last_success_timestamp_seconds` | gauge | None | Most recent successful account read. |
| `cwo_codex_account_reset_credits_available` | gauge | None | Available earned reset credits. |
| `cwo_codex_account_window_used_percent` | gauge | `window` | Reported used allowance, percent. |
| `cwo_codex_account_window_minutes` | gauge | `window` | Reported account-window duration, minutes. |
| `cwo_codex_account_window_reset_timestamp_seconds` | gauge | `window` | Scheduled reset, Unix seconds. |

## CWO Dispatches

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

## State Codes

Session activity: `0` Unknown, `1` Working, `2` Waiting, `3` Stopped / failed, `4` No recent signal.
Session usage: `0` unknown, `1` recorded, `2` runtime-only, `3` conflicted, `4` partial.

CWO field/token state: `0` unavailable, `1` present, `2` runtime-normalized, `3` invalid, `4` partial, `5` unqualified, `6` clock gap, `7` overflow.
CWO coverage: `0` unknown, `1` observed with no known gap, `2` known gap, `3` accounting conflict.

See [Reading the Values](../dashboards/reading-values.md), [Collector Health](collector-health.md), and the [dispatch contract](../../scripts/traceonaut/observability_contract.py) for scope and remaining enumerations.

## CWO Workflow Audits

These optional gauges come from configured CWO audit JSONL, independently of the observed-dispatch ledger. Event timestamps describe when the source recorded an event. `skipped_records` and `source_errors` describe the latest scan, rather than cumulative failures.

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
| `cwo_audit_event_timestamp_seconds` | gauge | `event_id`, `event_type` | Source Unix timestamp of one unique CWO audit event; not a job or token count. |

Skip reasons: `invalid_json`, `unsupported_record`, `invalid_hash`, `invalid_timestamp`, `future_timestamp`, `oversized_line`, `partial_line`. Unknown valid event types use `other`. The content hash supports consistency and deduplication, not proof that a job executed.

## CWO-Associated Sessions

These optional gauges come from Codex rollout records with `--cwo-sessions`. The dashboard joins association with the existing session metrics; no second token ledger is created.

| Name | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `cwo_codex_cwo_scan_timestamp_seconds` | gauge | None | Latest CWO session source scan attempt, Unix seconds. |
| `cwo_codex_cwo_scan_ready` | gauge | None | Selected session sources scanned without pending files, access errors or retained gaps. |
| `cwo_codex_cwo_pending_files` | gauge | None | Selected rollout files still awaiting CWO association scanning. |
| `cwo_codex_cwo_source_errors` | gauge | None | CWO source access failures in the latest scan. |
| `cwo_codex_cwo_source_gaps` | gauge | None | Persisted malformed or oversized candidate records and command conflicts. |
| `cwo_codex_cwo_limit_reached` | gauge | None | Source-file or completed-command export cap reached. |
| `cwo_codex_session_cwo_association_timestamp_seconds` | gauge | `project_id`, `session_id`, `source` | First CWO association: structured skill block, direct helper command, or parent session; not exclusive token attribution. |
| `cwo_codex_cwo_command_timestamp_seconds` | gauge | `project_id`, `session_id`, `observation_id`, `tool`, `outcome` | Source completion time of a supported direct CWO helper command. |
| `cwo_codex_cwo_command_duration_seconds` | gauge | `project_id`, `session_id`, `observation_id`, `tool`, `outcome` | Reported duration of a supported direct CWO helper command. |

Association sources: `skill_block`, `tool_execution`, `parent_session`. Command outcomes: `completed`, `failed`, `unknown`. Supported helper names: `build_contractor_packet`, `close_bead_with_summary`, `coach_prompt`, `dispatch_work`, `evaluate_return`, `normalize_contractor_return`, `render_execution_status_report`, `route_work`, `run_checked_command`, `supervise_native_pool`, `supervise_native_worker`, `validate_operator_handoff`, `validate_run_readiness_plan`.
