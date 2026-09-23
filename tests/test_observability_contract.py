from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_contract import (  # noqa: E402
    AcknowledgementProvenance,
    ConfigurationProvenance,
    CoverageGap,
    CoverageState,
    FieldState,
    HealthReason,
    LIFECYCLE_CODES,
    LifecycleState,
    METRIC_FAMILIES,
    PublicationState,
    SAFE_INTEGER_MAX,
    TOKEN_KIND_PROVENANCE,
    TokenKind,
    TokenProvenance,
    aggregate_token_state,
    calculate_declared_allowance,
    normalize_dispatch_observation,
    normalize_model_completion,
    normalize_project_registration,
    normalize_runtime_binding,
    normalize_telemetry_health,
    normalize_token_usage,
    numeric_for_export,
    token_for_export,
    validate_lifecycle_transition,
)


HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
RUNTIME_OWNER_ID = "22222222-2222-4222-8222-222222222222"
DISPATCH_ID = "33333333-3333-4333-8333-333333333333"
AGENT_ID = "44444444-4444-4444-8444-444444444444"
CONNECTION_ID = "55555555-5555-4555-8555-555555555555"
BINDING_ID = "66666666-6666-4666-8666-666666666666"
CLOCK_EPOCH_ID = "77777777-7777-4777-8777-777777777777"

MODELS = {"gpt-5.6-sol", "gpt-5.6-luna"}
EFFORTS = {"low", "high", "xhigh"}
EXECUTORS = {"frontier_architect", "native_worker"}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_input() -> dict:
    return {
        "project_id": PROJECT_ID,
        "owning_principal_id": 1000,
        "runtime_owner_id": RUNTIME_OWNER_ID,
        "state": "enabled",
        "generation": 1,
        "resource_limits": {
            "max_frame_bytes": 8 * 1024 * 1024,
            "max_json_depth": 32,
            "max_normalized_record_bytes": 16 * 1024,
            "max_pending_records": 1024,
            "max_queue_records": 4096,
            "pending_binding_deadline_seconds": 60,
            "max_registrations": 8,
            "max_disk_bytes": 1024 * 1024 * 1024,
        },
    }


def dispatch_input() -> dict:
    return {
        "project_id": PROJECT_ID,
        "dispatch_id": DISPATCH_ID,
        "agent_id": AGENT_ID,
        "packet_ref": "packet-7",
        "packet_sha256": sha("packet"),
        "supervisor_submission_ref": "submission-7",
        "requested_executor": "native_worker",
        "requested_model": "gpt-5.6-sol",
        "requested_effort": "xhigh",
        "declared_cycle_allowance": 14,
        "declared_elapsed_allowance_seconds": 400,
        "enforced_tool_call_limit": 40,
        "enforced_runtime_limit_seconds": 900,
        "lifecycle_state": "completed",
        "timing": {
            "clock_source": "supervisor-monotonic",
            "clock_epoch_id": CLOCK_EPOCH_ID,
            "provenance": "supervisor-lifecycle-receipt",
            "submitted_seconds": 100,
            "acknowledged_seconds": 101,
            "running_seconds": 102,
            "terminal_seconds": 510,
            "elapsed_seconds": 410,
            "elapsed_state": int(FieldState.PRESENT),
        },
        "coverage_state": int(CoverageState.OBSERVED_NO_KNOWN_GAP),
    }


def binding_input() -> dict:
    return {
        "binding_id": BINDING_ID,
        "project_id": PROJECT_ID,
        "registration_generation": 1,
        "connection_id": CONNECTION_ID,
        "dispatch_id": DISPATCH_ID,
        "agent_id": AGENT_ID,
        "packet_ref": "packet-7",
        "packet_sha256": sha("packet"),
        "supervisor_submission_ref": "submission-7",
        "thread_fingerprint": sha("thread"),
        "turn_fingerprint": sha("turn"),
        "acknowledgement_receipt_sha256": sha("ack"),
        "acknowledgement_provenance": AcknowledgementProvenance.TRUSTED_CONTROLLER_RECEIPT.value,
        "state": "active",
        "configured_model": "gpt-5.6-sol",
        "configured_model_provenance": ConfigurationProvenance.ACKNOWLEDGED_THREAD_CONFIGURATION.value,
        "configured_effort": "xhigh",
        "configured_effort_provenance": ConfigurationProvenance.ACKNOWLEDGED_THREAD_CONFIGURATION.value,
    }


def completion_input(usage: object = None) -> dict:
    return {
        "binding_id": BINDING_ID,
        "project_id": PROJECT_ID,
        "dispatch_id": DISPATCH_ID,
        "thread_fingerprint": sha("thread"),
        "turn_fingerprint": sha("turn"),
        "response_fingerprint": sha("response"),
        "cycle_ordinal": 1,
        "usage": usage,
        "purpose": "model-response",
        "source_compatibility_sha256": sha("codex-0.154.0-schema"),
        "observed_at_seconds": 1_789_000_000.25,
        "runtime_emitted_at_seconds": None,
    }


def health_input() -> dict:
    return {
        "project_id": PROJECT_ID,
        "connection_state": "connected",
        "schema_state": "compatible",
        "ledger_state": "healthy",
        "queue_state": "healthy",
        "disk_state": "healthy",
        "publication_state": int(PublicationState.AVAILABLE),
        "reason_counts": {"source-disconnected": 2},
        "unassigned_count": 1,
        "conflict_count": 0,
        "rejected_count": 0,
        "queue_depth": 3,
        "ledger_bytes": 4096,
        "last_event_timestamp_seconds": 1_789_000_000.5,
        "known_coverage_gaps": ["source-disconnect"],
    }


class NumericAndAllowanceTests(unittest.TestCase):
    def test_declared_allowance_examples_are_display_only(self) -> None:
        cycles = calculate_declared_allowance(14, 15)
        elapsed = calculate_declared_allowance(400, 410)

        self.assertEqual((cycles.remaining, cycles.overrun), (0, 1))
        self.assertEqual((elapsed.remaining, elapsed.overrun), (0, 10))
        self.assertFalse(cycles.lower_bound_overrun)

    def test_gap_exposes_only_a_known_lower_bound_overrun(self) -> None:
        below = calculate_declared_allowance(14, 10, coverage=CoverageState.KNOWN_GAP)
        above = calculate_declared_allowance(14, 17, coverage=CoverageState.KNOWN_GAP)

        self.assertEqual(below, type(below)(None, None, False))
        self.assertEqual(above, type(above)(None, 3, True))

    def test_large_integer_is_retained_and_only_overflows_at_export(self) -> None:
        exact = SAFE_INTEGER_MAX + 11
        usage = normalize_token_usage(
            {
                "input": exact,
                "cached_input": 0,
                "cache_write_input": 0,
                "output": 1,
                "reasoning_output": 0,
                "total": exact + 1,
            }
        )
        assert usage is not None

        self.assertEqual(usage["input"]["value"], exact)
        self.assertEqual(usage["input"]["state"], int(FieldState.PRESENT))
        self.assertEqual(token_for_export(usage["input"]), (None, FieldState.OVERFLOW))
        self.assertEqual(
            numeric_for_export(SAFE_INTEGER_MAX),
            (SAFE_INTEGER_MAX, FieldState.PRESENT),
        )

    def test_boolean_and_nonfinite_numbers_are_not_valid_measurements(self) -> None:
        with self.assertRaises(ValueError):
            numeric_for_export(True)
        with self.assertRaises(ValueError):
            calculate_declared_allowance(14, float("inf"))


class TokenUsageTests(unittest.TestCase):
    def test_nullable_usage_is_distinct_from_an_invalid_usage_payload(self) -> None:
        self.assertIsNone(normalize_token_usage(None))
        invalid = normalize_token_usage("not-a-mapping")
        assert invalid is not None

        self.assertEqual(
            {entry["state"] for entry in invalid.values()},
            {int(FieldState.INVALID)},
        )

    def test_optional_subsets_keep_unknown_presence_for_zero_and_nonzero(self) -> None:
        usage = normalize_token_usage(
            {
                "input": 20,
                "cached_input": 10,
                "cache_write_input": 0,
                "output": 5,
                "reasoning_output": 2,
                "total": 25,
            }
        )
        assert usage is not None

        for kind in ("cached_input", "cache_write_input", "reasoning_output"):
            self.assertEqual(usage[kind]["state"], int(FieldState.RUNTIME_NORMALIZED))
            self.assertEqual(
                usage[kind]["provenance"],
                TokenProvenance.RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN.value,
            )
        self.assertEqual(usage["input"]["state"], int(FieldState.PRESENT))

    def test_qualified_relationships_are_invalidated_without_losing_identity(
        self,
    ) -> None:
        usage = normalize_token_usage(
            {
                "input": 5,
                "cached_input": 6,
                "cache_write_input": 999,
                "output": 2,
                "reasoning_output": 3,
                "total": 8,
                "provider_metadata": "discard me",
            }
        )
        assert usage is not None

        self.assertEqual(usage["cached_input"]["state"], int(FieldState.INVALID))
        self.assertEqual(usage["reasoning_output"]["state"], int(FieldState.INVALID))
        self.assertEqual(usage["total"]["state"], int(FieldState.INVALID))
        self.assertEqual(
            usage["cache_write_input"]["state"], int(FieldState.RUNTIME_NORMALIZED)
        )
        self.assertNotIn("provider_metadata", usage)

    def test_token_inventory_has_fixed_provenance_for_every_kind(self) -> None:
        self.assertEqual(set(TOKEN_KIND_PROVENANCE), set(TokenKind))
        self.assertIs(
            TOKEN_KIND_PROVENANCE[TokenKind.CACHED_INPUT],
            TokenProvenance.RUNTIME_NORMALIZED_UPSTREAM_PRESENCE_UNKNOWN,
        )

    def test_dispatch_token_state_distinguishes_partial_and_normalized_sums(
        self,
    ) -> None:
        self.assertIs(
            aggregate_token_state(
                TokenKind.INPUT, contributing_cycles=2, observed_cycles=3
            ),
            FieldState.PARTIAL,
        )
        self.assertIs(
            aggregate_token_state(
                TokenKind.CACHED_INPUT, contributing_cycles=3, observed_cycles=3
            ),
            FieldState.RUNTIME_NORMALIZED,
        )
        self.assertIs(
            aggregate_token_state(
                TokenKind.OUTPUT, contributing_cycles=3, observed_cycles=3
            ),
            FieldState.PRESENT,
        )
        with self.assertRaises(ValueError):
            aggregate_token_state(
                TokenKind.TOTAL, contributing_cycles=4, observed_cycles=3
            )


class RecordNormalizationTests(unittest.TestCase):
    def test_project_registration_applies_finite_v1_resource_bounds(self) -> None:
        source = project_input()
        source["local_path"] = "/private/project"
        normalized = normalize_project_registration(source)

        self.assertEqual(normalized["record_type"], "project_registration.v1")
        self.assertNotIn("local_path", normalized)
        source["resource_limits"]["max_queue_records"] = 4097
        with self.assertRaisesRegex(ValueError, "v1 safety envelope"):
            normalize_project_registration(source)

    def test_dispatch_keeps_declared_and_enforced_units_separate(self) -> None:
        source = dispatch_input()
        source["requested_model"] = "free form model/path"
        source["prompt"] = "must not persist"
        normalized = normalize_dispatch_observation(
            source,
            executor_vocabulary=EXECUTORS,
            model_vocabulary=MODELS,
            effort_vocabulary=EFFORTS,
        )

        self.assertEqual(normalized["requested_model"], "unknown")
        self.assertEqual(normalized["requested_effort"], "xhigh")
        self.assertEqual(normalized["declared_cycle_allowance"], 14)
        self.assertEqual(normalized["declared_elapsed_allowance_seconds"], 400)
        self.assertEqual(normalized["enforced_tool_call_limit"], 40)
        self.assertEqual(normalized["enforced_runtime_limit_seconds"], 900)
        self.assertNotIn("prompt", normalized)
        self.assertNotIn("agent_model_calls", normalized)

    def test_dispatch_preserves_requested_model_and_effort_missingness(self) -> None:
        source = dispatch_input()
        source["requested_model"] = None
        source["requested_effort"] = None
        normalized = normalize_dispatch_observation(
            source,
            executor_vocabulary=EXECUTORS,
            model_vocabulary=MODELS,
            effort_vocabulary=EFFORTS,
        )

        self.assertIsNone(normalized["requested_model"])
        self.assertIsNone(normalized["requested_effort"])

    def test_binding_keeps_configured_model_and_effort_independent(self) -> None:
        source = binding_input()
        source["configured_model"] = None
        source["configured_model_provenance"] = None
        normalized = normalize_runtime_binding(
            source,
            model_vocabulary=MODELS,
            effort_vocabulary=EFFORTS,
        )

        self.assertIsNone(normalized["configured_model"])
        self.assertIsNone(normalized["configured_model_provenance"])
        self.assertEqual(normalized["configured_effort"], "xhigh")
        self.assertEqual(
            normalized["configured_effort_provenance"],
            "acknowledged-thread-configuration",
        )

    def test_completion_has_no_actual_model_timing_attempt_or_tool_link_fields(
        self,
    ) -> None:
        source = completion_input(
            {
                "input": 3,
                "cached_input": 0,
                "cache_write_input": 0,
                "output": 2,
                "reasoning_output": 0,
                "total": 5,
            }
        )
        source.update(
            {
                "actual_model": "invented",
                "actual_effort": "invented",
                "request_attempts": 7,
                "response_duration": 1.5,
                "tool_response_link": "invented",
                "raw_response_id": "sensitive-source-id",
            }
        )
        normalized = normalize_model_completion(source)

        self.assertEqual(normalized["record_type"], "model_completion.v1")
        for excluded in (
            "actual_model",
            "actual_effort",
            "request_attempts",
            "response_duration",
            "tool_response_link",
            "raw_response_id",
        ):
            self.assertNotIn(excluded, normalized)

    def test_valid_completion_identity_survives_invalid_usage(self) -> None:
        normalized = normalize_model_completion(completion_input({"input": False}))

        self.assertEqual(normalized["response_fingerprint"], sha("response"))
        self.assertEqual(normalized["cycle_ordinal"], 1)
        self.assertEqual(normalized["usage"]["input"]["state"], int(FieldState.INVALID))

    def test_health_reasons_are_bounded_and_missing_counts_become_zero(self) -> None:
        normalized = normalize_telemetry_health(health_input())

        self.assertEqual(normalized["reason_counts"]["source-disconnected"], 2)
        self.assertEqual(
            set(normalized["reason_counts"]), {reason.value for reason in HealthReason}
        )
        self.assertEqual(normalized["reason_counts"]["invalid-usage"], 0)

        source = health_input()
        source["reason_counts"]["free-form-error"] = 1
        with self.assertRaisesRegex(ValueError, "unsupported reasons"):
            normalize_telemetry_health(source)

    def test_collector_failures_have_distinct_bounded_reasons_and_gaps(self) -> None:
        source = health_input()
        expected = {
            "collector-write-failure",
            "collector-hook-failure",
            "shutdown-with-pending-events",
        }
        source["reason_counts"].update({reason: 1 for reason in expected})
        source["known_coverage_gaps"].extend(sorted(expected))

        normalized = normalize_telemetry_health(source)

        self.assertEqual(
            expected,
            {
                HealthReason.COLLECTOR_WRITE_FAILURE.value,
                HealthReason.COLLECTOR_HOOK_FAILURE.value,
                HealthReason.SHUTDOWN_PENDING.value,
            },
        )
        self.assertEqual(
            expected,
            {
                CoverageGap.COLLECTOR_WRITE_FAILURE.value,
                CoverageGap.COLLECTOR_HOOK_FAILURE.value,
                CoverageGap.SHUTDOWN_PENDING.value,
            },
        )
        self.assertTrue(expected.issubset(normalized["known_coverage_gaps"]))
        self.assertTrue(
            all(normalized["reason_counts"][reason] == 1 for reason in expected)
        )


class LifecycleAndMetricInventoryTests(unittest.TestCase):
    def test_lifecycle_codes_and_transitions_match_the_frozen_contract(self) -> None:
        self.assertEqual(
            [LIFECYCLE_CODES[state] for state in LifecycleState], list(range(8))
        )
        validate_lifecycle_transition("submitted", "acknowledged")
        validate_lifecycle_transition("submitted", "failed")
        validate_lifecycle_transition("submitted", "completed")
        validate_lifecycle_transition("acknowledged", "interrupted")
        validate_lifecycle_transition("running", "control-lost")
        validate_lifecycle_transition("completed", "completed")
        with self.assertRaises(ValueError):
            validate_lifecycle_transition("completed", "running")

    def test_metric_families_use_exact_names_and_no_sensitive_labels(self) -> None:
        expected = {
            "cwo_dispatch_info",
            "cwo_dispatch_configured_model_info",
            "cwo_dispatch_configured_effort_info",
            "cwo_dispatch_snapshot_revision",
            "cwo_dispatch_state",
            "cwo_agent_state",
            "cwo_dispatch_completed_cycles_total",
            "cwo_cycle_present",
            "cwo_cycle_observed_timestamp_seconds",
            "cwo_cycle_tokens",
            "cwo_dispatch_observed_tokens",
            "cwo_cycle_token_state",
            "cwo_dispatch_token_state",
            "cwo_dispatch_token_cycles",
            "cwo_dispatch_elapsed_seconds",
            "cwo_dispatch_declared_cycle_allowance",
            "cwo_dispatch_declared_cycle_remaining",
            "cwo_dispatch_declared_cycle_overrun",
            "cwo_dispatch_declared_elapsed_allowance_seconds",
            "cwo_dispatch_declared_elapsed_remaining_seconds",
            "cwo_dispatch_declared_elapsed_overrun_seconds",
            "cwo_dispatch_enforced_tool_call_limit",
            "cwo_dispatch_enforced_runtime_limit_seconds",
            "cwo_dispatch_field_state",
            "cwo_dispatch_retry_notices_total",
            "cwo_dispatch_turn_outcomes_total",
            "cwo_dispatch_tool_events_total",
            "cwo_telemetry_events_total",
            "cwo_telemetry_component_state",
            "cwo_telemetry_last_event_timestamp_seconds",
            "cwo_telemetry_queue_depth",
            "cwo_telemetry_ledger_bytes",
            "cwo_telemetry_publication_state",
            "cwo_telemetry_publication_pending_dispatches",
            "cwo_dispatch_coverage_state",
        }
        forbidden_labels = {
            "path",
            "packet_sha256",
            "thread_id",
            "turn_id",
            "response_id",
            "error",
        }

        self.assertEqual(set(METRIC_FAMILIES), expected)
        for name, family in METRIC_FAMILIES.items():
            self.assertTrue(name.startswith("cwo_"))
            self.assertTrue(forbidden_labels.isdisjoint(family.labels))
        self.assertEqual(
            METRIC_FAMILIES["cwo_cycle_tokens"].labels,
            ("project_id", "dispatch_id", "cycle_ordinal", "token_kind"),
        )


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is not installed")
class SchemaCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from jsonschema import Draft202012Validator

        cls.validator_type = Draft202012Validator

    def assert_schema_accepts(self, filename: str, value: dict) -> None:
        schema = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        self.validator_type.check_schema(schema)
        errors = sorted(self.validator_type(schema).iter_errors(value), key=str)
        self.assertEqual(errors, [])

    def test_normalized_records_match_their_additive_schemas(self) -> None:
        records = {
            "supervisor-project-registration-v1.schema.json": normalize_project_registration(
                project_input()
            ),
            "supervisor-dispatch-observation-v1.schema.json": normalize_dispatch_observation(
                dispatch_input(),
                executor_vocabulary=EXECUTORS,
                model_vocabulary=MODELS,
                effort_vocabulary=EFFORTS,
            ),
            "supervisor-runtime-binding-v1.schema.json": normalize_runtime_binding(
                binding_input(),
                model_vocabulary=MODELS,
                effort_vocabulary=EFFORTS,
            ),
            "supervisor-model-completion-v1.schema.json": normalize_model_completion(
                completion_input(
                    {
                        "input": 9,
                        "cached_input": 4,
                        "cache_write_input": 0,
                        "output": 1,
                        "reasoning_output": 0,
                        "total": 10,
                    }
                )
            ),
            "supervisor-telemetry-health-v1.schema.json": normalize_telemetry_health(
                health_input()
            ),
        }
        for filename, record in records.items():
            with self.subTest(schema=filename):
                self.assert_schema_accepts(filename, record)


if __name__ == "__main__":
    unittest.main()
