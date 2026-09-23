from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_adapter import AppServerCompletionAdapter  # noqa: E402
from traceonaut.observability_contract import (  # noqa: E402
    CoverageGap,
    CoverageState,
    FieldState,
    PublicationState,
    SAFE_INTEGER_MAX,
)
from traceonaut.observability_exporter import build_samples  # noqa: E402
from traceonaut.observability_ledger import (  # noqa: E402
    DATABASE_NAME,
    ObservabilityLedger,
    ObservabilityLedgerError,
    SQLITE_CAPACITY_FIXED_OVERHEAD_BYTES,
)


def identifier() -> str:
    return str(uuid.uuid4())


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ObservabilityLedgerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.project_id = identifier()
        self.runtime_owner_id = identifier()
        self.dispatch_id = identifier()
        self.agent_id = identifier()
        self.connection_id = identifier()
        self.binding_id = identifier()
        self.clock_epoch_id = identifier()
        self.ledger = ObservabilityLedger(
            self.state,
            executor_vocabulary={"native_worker"},
            model_vocabulary={"qualified-model"},
            effort_vocabulary={"xhigh"},
        )
        self.ledger.register_project(self.registration())

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def registration(self) -> dict:
        return {
            "project_id": self.project_id,
            "owning_principal_id": os.geteuid(),
            "runtime_owner_id": self.runtime_owner_id,
            "state": "enabled",
            "generation": 1,
            "resource_limits": {
                "max_frame_bytes": 8 * 1024 * 1024,
                "max_json_depth": 32,
                "max_normalized_record_bytes": 16 * 1024,
                "max_pending_records": 32,
                "max_queue_records": 32,
                "pending_binding_deadline_seconds": 60,
                "max_registrations": 8,
                "max_disk_bytes": 32 * 1024 * 1024,
            },
        }

    def observation(self) -> dict:
        return {
            "project_id": self.project_id,
            "dispatch_id": self.dispatch_id,
            "agent_id": self.agent_id,
            "packet_ref": "packet-7",
            "packet_sha256": sha("packet"),
            "supervisor_submission_ref": "submission-7",
            "requested_executor": "native_worker",
            "requested_model": "qualified-model",
            "requested_effort": "xhigh",
            "declared_cycle_allowance": 4,
            "declared_elapsed_allowance_seconds": 60,
            "enforced_tool_call_limit": 10,
            "enforced_runtime_limit_seconds": 90,
            "lifecycle_state": "submitted",
            "timing": {
                "clock_source": "supervisor-monotonic",
                "clock_epoch_id": self.clock_epoch_id,
                "provenance": "supervisor-lifecycle-receipt",
                "submitted_seconds": 10,
                "acknowledged_seconds": None,
                "running_seconds": None,
                "terminal_seconds": None,
                "elapsed_seconds": None,
                "elapsed_state": int(FieldState.UNAVAILABLE),
            },
            "coverage_state": int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        }

    def adapter(self) -> AppServerCompletionAdapter:
        return AppServerCompletionAdapter(
            self.ledger,
            project_id=self.project_id,
            registration_generation=1,
            connection_id=self.connection_id,
            source_compatibility_sha256=sha("qualified-source"),
            auto_start=False,
        )

    def bind(self, adapter: AppServerCompletionAdapter) -> None:
        self.ledger.bind_runtime(
            {
                "binding_id": self.binding_id,
                "project_id": self.project_id,
                "registration_generation": 1,
                "connection_id": self.connection_id,
                "dispatch_id": self.dispatch_id,
                "agent_id": self.agent_id,
                "packet_ref": "packet-7",
                "packet_sha256": sha("packet"),
                "supervisor_submission_ref": "submission-7",
                "thread_fingerprint": adapter.fingerprint("thread", "thread-1"),
                "turn_fingerprint": adapter.fingerprint("turn", "turn-1"),
                "acknowledgement_receipt_sha256": sha("ack"),
                "acknowledgement_provenance": "trusted-controller-receipt",
                "state": "active",
                "configured_model": "qualified-model",
                "configured_model_provenance": "acknowledged-thread-configuration",
                "configured_effort": "xhigh",
                "configured_effort_provenance": "acknowledged-thread-configuration",
            }
        )

    def test_compatible_binding_promotes_unknown_coverage_but_never_a_gap(self) -> None:
        observation = self.observation()
        observation["coverage_state"] = int(CoverageState.UNKNOWN)
        self.ledger.record_submission(observation)
        adapter = self.adapter()

        self.bind(adapter)
        dispatch = self.ledger.snapshot()["projects"][0]["dispatches"][0]
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"],
            int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        )

        second = self.observation()
        second["dispatch_id"] = identifier()
        second["agent_id"] = identifier()
        second["coverage_state"] = int(CoverageState.UNKNOWN)
        self.ledger.mark_gap(self.project_id, CoverageGap.QUEUE_OVERFLOW)
        self.ledger.record_submission(second)
        second_binding = {
            "binding_id": identifier(),
            "project_id": self.project_id,
            "registration_generation": 1,
            "connection_id": self.connection_id,
            "dispatch_id": second["dispatch_id"],
            "agent_id": second["agent_id"],
            "packet_ref": second["packet_ref"],
            "packet_sha256": second["packet_sha256"],
            "supervisor_submission_ref": second["supervisor_submission_ref"],
            "thread_fingerprint": adapter.fingerprint("thread", "thread-2"),
            "turn_fingerprint": adapter.fingerprint("turn", "turn-2"),
            "acknowledgement_receipt_sha256": sha("ack-2"),
            "acknowledgement_provenance": "trusted-controller-receipt",
            "state": "active",
            "configured_model": "qualified-model",
            "configured_model_provenance": "acknowledged-thread-configuration",
            "configured_effort": "xhigh",
            "configured_effort_provenance": "acknowledged-thread-configuration",
        }
        self.ledger.bind_runtime(second_binding)
        dispatches = self.ledger.snapshot()["projects"][0]["dispatches"]
        by_id = {row["observation"]["dispatch_id"]: row for row in dispatches}
        self.assertEqual(
            by_id[second["dispatch_id"]]["aggregate"]["coverage_state"],
            int(CoverageState.KNOWN_GAP),
        )

    def test_cross_epoch_terminal_receipt_keeps_lifecycle_and_suppresses_duration(
        self,
    ) -> None:
        self.ledger.record_submission(self.observation())
        other_epoch = identifier()
        timing = {
            "clock_source": "supervisor-monotonic",
            "clock_epoch_id": other_epoch,
            "provenance": "supervisor-lifecycle-receipt",
            "submitted_seconds": 1,
            "acknowledged_seconds": 2,
            "running_seconds": 3,
            "terminal_seconds": 410,
            "elapsed_seconds": 409,
            "elapsed_state": int(FieldState.PRESENT),
        }

        observed = self.ledger.record_lifecycle(
            self.dispatch_id,
            lifecycle_state="completed",
            timing=timing,
            receipt_sha256=sha("cross-epoch-terminal"),
        )

        self.assertEqual(observed["lifecycle_state"], "completed")
        self.assertEqual(observed["timing"]["clock_epoch_id"], self.clock_epoch_id)
        self.assertEqual(observed["timing"]["submitted_seconds"], 10)
        self.assertIsNone(observed["timing"]["terminal_seconds"])
        self.assertIsNone(observed["timing"]["elapsed_seconds"])
        self.assertEqual(observed["timing"]["elapsed_state"], int(FieldState.CLOCK_GAP))
        self.assertEqual(observed["coverage_state"], int(CoverageState.KNOWN_GAP))
        health = self.ledger.snapshot()["projects"][0]["health"]
        self.assertIn("timing-epoch-gap", health["known_coverage_gaps"])

    def test_revoked_project_does_not_hide_other_project_snapshot(self) -> None:
        other_project = identifier()
        other = self.registration()
        other["project_id"] = other_project
        other["runtime_owner_id"] = identifier()
        self.ledger.register_project(other)
        revoked = self.registration()
        revoked["generation"] = 2
        revoked["state"] = "revoked"
        self.ledger.register_project(revoked)

        self.assertEqual(self.ledger.expire_pending(), 0)
        snapshot = self.ledger.snapshot()
        by_project = {
            row["registration"]["project_id"]: row for row in snapshot["projects"]
        }
        self.assertEqual(
            by_project[self.project_id]["registration"]["state"], "revoked"
        )
        self.assertEqual(by_project[other_project]["registration"]["state"], "enabled")
        samples, _manifests = build_samples(snapshot)
        self.assertNotIn(
            self.project_id,
            {dict(sample.labels).get("project_id") for sample in samples},
        )
        self.assertIn(
            other_project, {dict(sample.labels).get("project_id") for sample in samples}
        )

    def test_long_reader_cannot_make_wal_exceed_registered_total_capacity(self) -> None:
        capacity_state = self.root / "capacity-state"
        registration = self.registration()
        registration["project_id"] = identifier()
        registration["runtime_owner_id"] = identifier()
        registration["resource_limits"]["max_disk_bytes"] = (
            SQLITE_CAPACITY_FIXED_OVERHEAD_BYTES + 2 * 1024 * 1024
        )
        ledger = ObservabilityLedger(
            capacity_state,
            executor_vocabulary={"native_worker"},
            model_vocabulary={"qualified-model"},
            effort_vocabulary={"xhigh"},
        )
        self.addCleanup(ledger.close)
        ledger.register_project(registration)
        reader = sqlite3.connect(capacity_state / DATABASE_NAME)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM dispatches").fetchone()

        retained = 0
        with self.assertRaisesRegex(
            ObservabilityLedgerError, "observability-capacity-blocked"
        ):
            for index in range(2_000):
                observation = self.observation()
                observation["project_id"] = registration["project_id"]
                observation["dispatch_id"] = identifier()
                observation["agent_id"] = identifier()
                observation["packet_ref"] = f"capacity-packet-{index}"
                observation["supervisor_submission_ref"] = f"capacity-submit-{index}"
                ledger.record_submission(observation)
                retained += 1

        snapshot = ledger.snapshot()
        project = snapshot["projects"][0]
        self.assertEqual(len(project["dispatches"]), retained)
        self.assertGreater(retained, 0)
        self.assertEqual(project["health"]["disk_state"], "full")
        self.assertEqual(project["health"]["publication_state"], 4)
        self.assertEqual(project["health"]["ledger_state"], "fault")
        self.assertIn(
            "collector-write-failure",
            project["health"]["known_coverage_gaps"],
        )
        self.assertEqual(project["event_counts"]["lost"], 0)
        self.assertTrue(
            all(
                row["aggregate"]["coverage_state"] == int(CoverageState.KNOWN_GAP)
                for row in project["dispatches"]
            )
        )
        protected_paths = [
            capacity_state / DATABASE_NAME,
            capacity_state / f"{DATABASE_NAME}-wal",
            capacity_state / f"{DATABASE_NAME}-shm",
            capacity_state / "writer.lock",
            *(capacity_state / "keys").iterdir(),
        ]
        protected_bytes = sum(
            path.stat().st_size for path in protected_paths if path.exists()
        )
        self.assertLessEqual(
            protected_bytes,
            registration["resource_limits"]["max_disk_bytes"],
        )

    def test_checkpointed_growth_stops_before_sqlite_page_exhaustion(self) -> None:
        capacity_state = self.root / "checkpoint-capacity-state"
        registration = self.registration()
        registration["project_id"] = identifier()
        registration["runtime_owner_id"] = identifier()
        registration["resource_limits"]["max_disk_bytes"] = (
            SQLITE_CAPACITY_FIXED_OVERHEAD_BYTES + 2 * 1024 * 1024
        )
        ledger = ObservabilityLedger(
            capacity_state,
            executor_vocabulary={"native_worker"},
            model_vocabulary={"qualified-model"},
            effort_vocabulary={"xhigh"},
        )
        self.addCleanup(ledger.close)
        ledger.register_project(registration)

        retained = 0
        with self.assertRaisesRegex(
            ObservabilityLedgerError, "observability-capacity-blocked"
        ):
            for index in range(2_000):
                observation = self.observation()
                observation["project_id"] = registration["project_id"]
                observation["dispatch_id"] = identifier()
                observation["agent_id"] = identifier()
                observation["packet_ref"] = f"checkpoint-packet-{index}"
                observation["supervisor_submission_ref"] = f"checkpoint-submit-{index}"
                ledger.record_submission(observation)
                retained += 1

        project = ledger.snapshot()["projects"][0]
        self.assertEqual(len(project["dispatches"]), retained)
        self.assertGreater(retained, 0)
        self.assertEqual(project["health"]["disk_state"], "full")
        self.assertEqual(project["health"]["ledger_state"], "fault")
        self.assertEqual(project["health"]["publication_state"], 4)
        self.assertEqual(project["event_counts"]["lost"], 0)
        self.assertTrue(
            all(
                row["aggregate"]["coverage_state"] == int(CoverageState.KNOWN_GAP)
                for row in project["dispatches"]
            )
        )

    def test_existing_registration_limit_is_a_global_ceiling(self) -> None:
        limited_state = self.root / "limited-state"
        ledger = ObservabilityLedger(
            limited_state,
            executor_vocabulary={"native_worker"},
            model_vocabulary={"qualified-model"},
            effort_vocabulary={"xhigh"},
        )
        self.addCleanup(ledger.close)
        first = self.registration()
        first["project_id"] = identifier()
        first["runtime_owner_id"] = identifier()
        first["resource_limits"]["max_registrations"] = 1
        ledger.register_project(first)
        second = self.registration()
        second["project_id"] = identifier()
        second["runtime_owner_id"] = identifier()
        second["resource_limits"]["max_registrations"] = 8

        with self.assertRaisesRegex(
            ObservabilityLedgerError, "observability-registration-capacity"
        ):
            ledger.register_project(second)
        self.assertEqual(len(ledger.snapshot()["projects"]), 1)

    def test_malformed_supported_completion_disables_schema_and_marks_gap(self) -> None:
        observation = self.observation()
        observation["coverage_state"] = int(CoverageState.UNKNOWN)
        self.ledger.record_submission(observation)
        adapter = self.adapter()
        self.bind(adapter)
        self.assertEqual(
            self.dispatch_snapshot()["aggregate"]["coverage_state"],
            int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        )

        private_text = "must-never-be-persisted-malformed-body"
        self.assertEqual(
            adapter.try_submit(
                {
                    "method": "rawResponse/completed",
                    "params": private_text,
                }
            ),
            "rejected",
        )
        self.assertEqual(adapter.drain_once(), 0)

        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(project["health"]["schema_state"], "incompatible")
        self.assertIn("unsupported-source", project["health"]["known_coverage_gaps"])
        self.assertEqual(project["event_counts"]["rejected"], 1)
        self.assertEqual(
            project["dispatches"][0]["aggregate"]["coverage_state"],
            int(CoverageState.KNOWN_GAP),
        )
        self.assertNotIn(
            private_text.encode(), (self.state / DATABASE_NAME).read_bytes()
        )

    def test_malformed_event_precedes_expected_disconnect_and_gaps_terminal(
        self,
    ) -> None:
        observation = self.observation()
        observation["lifecycle_state"] = "completed"
        observation["coverage_state"] = int(CoverageState.UNKNOWN)
        observation["timing"].update(
            {
                "terminal_seconds": 20,
                "elapsed_seconds": 10,
                "elapsed_state": int(FieldState.PRESENT),
            }
        )
        self.ledger.record_submission(observation)
        adapter = self.adapter()
        self.bind(adapter)
        before = self.dispatch_snapshot()
        self.assertEqual(
            before["aggregate"]["coverage_state"],
            int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        )

        self.assertEqual(
            adapter.try_submit(
                {"method": "rawResponse/completed", "params": "malformed"}
            ),
            "rejected",
        )
        self.assertEqual(adapter.request_source_disconnected(expected=True), "queued")
        self.assertEqual(adapter.drain_once(), 1)

        project = self.ledger.snapshot()["projects"][0]
        dispatch = project["dispatches"][0]
        self.assertEqual(project["health"]["connection_state"], "disconnected")
        self.assertEqual(project["health"]["schema_state"], "incompatible")
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"], int(CoverageState.KNOWN_GAP)
        )
        self.assertGreater(dispatch["snapshot_revision"], before["snapshot_revision"])
        with adapter._counter_lock:
            self.assertFalse(adapter._malformed)
        adapter.close(mark_disconnected=False)

    def test_failed_terminal_projection_downgrades_expected_disconnect(self) -> None:
        observation = self.observation()
        observation["lifecycle_state"] = "completed"
        observation["coverage_state"] = int(CoverageState.UNKNOWN)
        observation["timing"].update(
            {
                "terminal_seconds": 20,
                "elapsed_seconds": 10,
                "elapsed_state": int(FieldState.PRESENT),
            }
        )
        self.ledger.record_submission(observation)
        adapter = self.adapter()
        self.bind(adapter)
        before = self.dispatch_snapshot()
        timestamp = 1_800_000_000.0
        revision = before["snapshot_revision"]
        samples = [
            {
                "name": "cwo_dispatch_snapshot_revision",
                "labels": {
                    "project_id": self.project_id,
                    "dispatch_id": self.dispatch_id,
                },
                "value": revision,
                "timestamp_seconds": timestamp,
            }
        ]
        manifest = self.ledger.stage_publication_manifest(
            self.dispatch_id,
            revision=revision,
            sample_timestamp_seconds=timestamp,
            samples=samples,
        )
        self.ledger.confirm_publication(
            self.dispatch_id,
            revision=revision,
            manifest_sha256=manifest,
            sample_timestamp_seconds=timestamp,
            confirmed_at_seconds=timestamp + 1,
        )
        self.assertFalse(self.dispatch_snapshot()["publication"]["pending"])
        self.assertEqual(adapter.try_submit(self.completion(10)), "queued")
        self.assertEqual(adapter.request_source_disconnected(expected=True), "queued")

        with mock.patch.object(
            self.ledger,
            "ingest_projection",
            side_effect=ObservabilityLedgerError("forced-write-failure"),
        ):
            self.assertEqual(adapter.drain_once(max_records=1), 1)

        project = self.ledger.snapshot()["projects"][0]
        dispatch = project["dispatches"][0]
        self.assertEqual(project["health"]["connection_state"], "connected")
        self.assertEqual(
            project["health"]["reason_counts"]["collector-write-failure"], 1
        )
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"], int(CoverageState.KNOWN_GAP)
        )
        self.assertGreater(dispatch["snapshot_revision"], before["snapshot_revision"])
        self.assertTrue(dispatch["publication"]["pending"])

        self.assertEqual(adapter.drain_once(max_records=1), 1)
        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(project["health"]["connection_state"], "disconnected")
        self.assertEqual(
            project["dispatches"][0]["aggregate"]["coverage_state"],
            int(CoverageState.KNOWN_GAP),
        )
        adapter.close(mark_disconnected=False)

    def dispatch_snapshot(self) -> dict:
        return self.ledger.snapshot()["projects"][0]["dispatches"][0]

    def completion(self, total_input: int) -> dict:
        return {
            "method": "rawResponse/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "responseId": "response-1",
                "usage": {
                    "inputTokens": total_input,
                    "cachedInputTokens": 0,
                    "cacheWriteInputTokens": 0,
                    "outputTokens": 1,
                    "reasoningOutputTokens": 0,
                    "totalTokens": total_input + 1,
                },
            },
        }

    def test_owned_stream_records_exact_completion_and_bounded_activity(self) -> None:
        self.ledger.record_submission(self.observation())
        adapter = self.adapter()
        self.bind(adapter)
        exact = SAFE_INTEGER_MAX + 9

        self.assertEqual(
            adapter.try_submit(self.completion(exact), received_unix_ns=10_000_000_000),
            "queued",
        )
        self.assertEqual(
            adapter.try_submit(
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "willRetry": True,
                        "message": "must not persist",
                    },
                },
                source_sequence=4,
            ),
            "queued",
        )
        self.assertEqual(
            adapter.try_submit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": "item-1",
                            "type": "commandExecution",
                            "status": "failed",
                            "command": "secret",
                        },
                    },
                }
            ),
            "queued",
        )
        self.assertEqual(adapter.drain_once(), 3)

        dispatch = self.dispatch_snapshot()
        self.assertEqual(dispatch["aggregate"]["completed_cycles"], 1)
        self.assertEqual(dispatch["aggregate"]["token_sums"]["input"], exact)
        self.assertEqual(
            dispatch["aggregate"]["token_states"]["input"], int(FieldState.PRESENT)
        )
        self.assertEqual(dispatch["activity"]["retry_notices"], 1)
        self.assertIn(
            {"category": "command", "lifecycle": "failed", "count": 1},
            dispatch["activity"]["tool_events"],
        )
        durable = (self.state / "observability.sqlite3").read_bytes()
        self.assertNotIn(b"must not persist", durable)
        self.assertNotIn(b"secret", durable)
        self.assertNotIn(b"thread-1", durable)
        adapter.close(mark_disconnected=False)

    def test_transport_replay_does_not_recount_or_change_ordinal(self) -> None:
        self.ledger.record_submission(self.observation())
        adapter = self.adapter()
        self.bind(adapter)
        message = self.completion(20)
        adapter.try_submit(message, received_unix_ns=10_000_000_000)
        adapter.drain_once()
        revision = self.dispatch_snapshot()["snapshot_revision"]
        adapter.try_submit(message, received_unix_ns=20_000_000_000)
        adapter.drain_once()

        dispatch = self.dispatch_snapshot()
        self.assertEqual(dispatch["snapshot_revision"], revision)
        self.assertEqual(dispatch["aggregate"]["completed_cycles"], 1)
        self.assertEqual(dispatch["cycles"][0]["cycle_ordinal"], 1)
        self.assertEqual(
            self.ledger.snapshot()["projects"][0]["event_counts"]["duplicate"],
            1,
        )
        adapter.close(mark_disconnected=False)

    def test_logical_identity_conflict_commits_fault_without_overwrite(self) -> None:
        self.ledger.record_submission(self.observation())
        adapter = self.adapter()
        self.bind(adapter)
        adapter.try_submit(self.completion(20))
        adapter.drain_once()
        accepted = self.dispatch_snapshot()
        response = accepted["cycles"][0]["response_fingerprint"]
        payload = {
            "project_id": self.project_id,
            "registration_generation": 1,
            "connection_id": self.connection_id,
            "thread_fingerprint": adapter.fingerprint("thread", "thread-1"),
            "turn_fingerprint": adapter.fingerprint("turn", "turn-1"),
            "event_fingerprint": adapter.fingerprint("event", "different-envelope"),
            "response_fingerprint": response,
            "observed_at_seconds": 30,
            "runtime_emitted_at_seconds": None,
            "purpose": "model-response",
            "usage": {
                "input": 25,
                "cached_input": 0,
                "cache_write_input": 0,
                "output": 1,
                "reasoning_output": 0,
                "total": 26,
            },
        }

        self.assertEqual(self.ledger.ingest_completion(payload), "conflict")
        dispatch = self.dispatch_snapshot()
        self.assertEqual(dispatch["aggregate"]["completed_cycles"], 1)
        self.assertEqual(dispatch["aggregate"]["token_sums"]["input"], 20)
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"],
            int(CoverageState.ACCOUNTING_CONFLICT),
        )
        self.assertEqual(dispatch["aggregate"]["conflict_count"], 1)
        adapter.close(mark_disconnected=False)

    def test_ledger_boundary_rejects_extra_content_fields(self) -> None:
        self.ledger.record_submission(self.observation())
        adapter = self.adapter()
        payload = {
            "project_id": self.project_id,
            "registration_generation": 1,
            "connection_id": self.connection_id,
            "thread_fingerprint": adapter.fingerprint("thread", "thread-1"),
            "turn_fingerprint": adapter.fingerprint("turn", "turn-1"),
            "event_fingerprint": adapter.fingerprint("event", "event-1"),
            "observed_at_seconds": 10,
            "command": "sensitive raw content",
        }
        with self.assertRaisesRegex(ObservabilityLedgerError, "event-fields-invalid"):
            self.ledger.ingest_projection("retry", payload)
        self.assertNotIn(
            b"sensitive raw content",
            (self.state / "observability.sqlite3").read_bytes(),
        )
        adapter.close(mark_disconnected=False)

    def test_missing_registered_key_enters_recovery_without_regeneration(self) -> None:
        key_path = next((self.state / "keys").iterdir())
        self.ledger.close()
        key_path.unlink()
        self.ledger = ObservabilityLedger(
            self.state,
            executor_vocabulary={"native_worker"},
            model_vocabulary={"qualified-model"},
            effort_vocabulary={"xhigh"},
        )

        self.assertFalse(key_path.exists())
        health = self.ledger.snapshot()["projects"][0]["health"]
        self.assertEqual(health["ledger_state"], "recovery")
        self.assertIn("key-loss", health["known_coverage_gaps"])
        self.assertEqual(health["reason_counts"]["key-unavailable"], 1)
        with self.assertRaisesRegex(ObservabilityLedgerError, "key-unavailable"):
            self.ledger.source_fingerprinter(self.project_id)

    def test_pending_completion_binds_late_and_disconnect_only_marks_gap(self) -> None:
        self.ledger.record_submission(self.observation())
        adapter = self.adapter()
        adapter.try_submit(self.completion(10))
        adapter.drain_once()
        self.assertEqual(self.dispatch_snapshot()["aggregate"]["completed_cycles"], 0)

        self.bind(adapter)
        self.assertEqual(self.dispatch_snapshot()["aggregate"]["completed_cycles"], 1)
        adapter.mark_source_disconnected()
        dispatch = self.dispatch_snapshot()
        self.assertEqual(dispatch["observation"]["lifecycle_state"], "submitted")
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"], int(CoverageState.KNOWN_GAP)
        )
        adapter.close(mark_disconnected=False)

    def test_reopen_recovers_connected_source_and_unbound_dispatch_as_gap(self) -> None:
        observation = self.observation()
        observation["coverage_state"] = int(CoverageState.UNKNOWN)
        self.ledger.record_submission(observation)
        adapter = self.adapter()
        adapter.close(mark_disconnected=False)
        self.ledger.close()
        self.ledger = ObservabilityLedger(
            self.state,
            executor_vocabulary={"native_worker"},
            model_vocabulary={"qualified-model"},
            effort_vocabulary={"xhigh"},
        )

        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(project["health"]["connection_state"], "disconnected")
        self.assertIn("crash-before-commit", project["health"]["known_coverage_gaps"])
        self.assertEqual(
            project["dispatches"][0]["aggregate"]["coverage_state"],
            int(CoverageState.KNOWN_GAP),
        )

    def test_generation_rollover_retires_old_source_and_rejects_late_event(
        self,
    ) -> None:
        self.ledger.record_submission(self.observation())
        adapter = self.adapter()
        self.bind(adapter)
        replacement = self.registration()
        replacement["generation"] = 2

        self.ledger.register_project(replacement)
        adapter.try_submit(self.completion(10))
        adapter.drain_once()

        project = self.ledger.snapshot()["projects"][0]
        dispatch = project["dispatches"][0]
        self.assertEqual(dispatch["aggregate"]["completed_cycles"], 0)
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"],
            int(CoverageState.KNOWN_GAP),
        )
        self.assertEqual(dispatch["bindings"][0]["state"], "revoked")
        self.assertEqual(project["health"]["connection_state"], "disabled")
        self.assertEqual(project["event_counts"]["rejected"], 1)
        adapter.close(mark_disconnected=False)

    def test_publication_confirmation_is_revision_exact_and_late_update_reopens(
        self,
    ) -> None:
        observation = self.observation()
        observation["lifecycle_state"] = "completed"
        observation["timing"].update(
            {
                "terminal_seconds": 20,
                "elapsed_seconds": 10,
                "elapsed_state": int(FieldState.PRESENT),
            }
        )
        self.ledger.record_submission(observation)
        adapter = self.adapter()
        self.bind(adapter)
        dispatch = self.dispatch_snapshot()
        revision = dispatch["snapshot_revision"]
        timestamp = 1_800_000_000.0
        samples = [
            {
                "name": "cwo_dispatch_snapshot_revision",
                "labels": {
                    "project_id": self.project_id,
                    "dispatch_id": self.dispatch_id,
                },
                "value": revision,
                "timestamp_seconds": timestamp,
            }
        ]
        manifest = self.ledger.stage_publication_manifest(
            self.dispatch_id,
            revision=revision,
            sample_timestamp_seconds=timestamp,
            samples=samples,
        )
        self.ledger.confirm_publication(
            self.dispatch_id,
            revision=revision,
            manifest_sha256=manifest,
            sample_timestamp_seconds=timestamp,
            confirmed_at_seconds=timestamp + 1,
        )
        self.assertFalse(self.dispatch_snapshot()["publication"]["pending"])
        self.ledger.set_publication_state(self.project_id, PublicationState.AVAILABLE)

        adapter.try_submit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )
        adapter.drain_once()
        changed = self.dispatch_snapshot()
        self.assertGreater(changed["snapshot_revision"], revision)
        self.assertTrue(changed["publication"]["pending"])
        self.assertEqual(
            self.ledger.snapshot()["projects"][0]["health"]["publication_state"],
            int(PublicationState.AVAILABLE),
        )
        adapter.close(mark_disconnected=False)

    def test_expected_disconnect_preserves_terminal_dispatch_coverage(self) -> None:
        observation = self.observation()
        observation["lifecycle_state"] = "completed"
        observation["timing"].update(
            {
                "terminal_seconds": 20,
                "elapsed_seconds": 10,
                "elapsed_state": int(FieldState.PRESENT),
            }
        )
        self.ledger.record_submission(observation)
        adapter = self.adapter()
        self.bind(adapter)

        self.assertEqual(adapter.try_submit(self.completion(10)), "queued")
        self.assertEqual(adapter.request_source_disconnected(expected=True), "queued")
        self.assertEqual(
            adapter.try_submit(self.completion(11)),
            "rejected",
        )
        self.assertEqual(adapter.drain_once(), 2)

        project = self.ledger.snapshot()["projects"][0]
        dispatch = project["dispatches"][0]
        self.assertEqual(dispatch["aggregate"]["completed_cycles"], 1)
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"],
            int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        )
        self.assertEqual(dispatch["binding"]["state"], "closed")
        self.assertEqual(project["event_counts"]["lost"], 0)
        adapter.close(mark_disconnected=False)

    def test_readonly_snapshot_never_takes_writer_or_generates_keys(self) -> None:
        self.ledger.record_submission(self.observation())
        before = sorted(path.name for path in (self.state / "keys").iterdir())
        snapshot = ObservabilityLedger.read_snapshot(self.state)
        after = sorted(path.name for path in (self.state / "keys").iterdir())

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(before, after)
        with self.assertRaisesRegex(ObservabilityLedgerError, "writer-already-active"):
            ObservabilityLedger(
                self.state,
                executor_vocabulary={"native_worker"},
                model_vocabulary={"qualified-model"},
                effort_vocabulary={"xhigh"},
            )


if __name__ == "__main__":
    unittest.main()
