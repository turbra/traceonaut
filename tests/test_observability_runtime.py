from __future__ import annotations

import json
import os
import queue
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_adapter import AppServerCompletionAdapter
from traceonaut.observability_exporter import build_samples, render_prometheus
from traceonaut.observability_ledger import ObservabilityLedger
from traceonaut.observability_runtime import OwnedRuntimeObserver
from test_observability_contract import (
    AGENT_ID,
    CONNECTION_ID,
    PROJECT_ID,
    project_input,
    sha,
)


class RuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = ObservabilityLedger(
            Path(self.temporary.name) / "state",
            executor_vocabulary=("native_worker",),
            model_vocabulary=("gpt-5.6-sol",),
            effort_vocabulary=("xhigh", "low"),
        )
        self.addCleanup(self.ledger.close)
        registration = project_input()
        registration["owning_principal_id"] = os.geteuid()
        self.ledger.register_project(registration)
        self.adapter = AppServerCompletionAdapter(
            self.ledger,
            project_id=PROJECT_ID,
            registration_generation=1,
            connection_id=CONNECTION_ID,
            source_compatibility_sha256=sha("qualified-source"),
            auto_start=False,
        )
        self.now = 100.0
        self.owner = OwnedRuntimeObserver(
            self.adapter, auto_start=False, monotonic=lambda: self.now
        )
        self.addCleanup(self.owner.close)

    def prepared(self):
        return self.owner.prepare_dispatch(
            agent_id=AGENT_ID,
            packet_sha256=sha("packet"),
            supervisor_submission_ref="controller-submission-1",
            requested_executor="native_worker",
            requested_model="gpt-5.6-sol",
            requested_effort="xhigh",
            declared_cycle_allowance=14,
            declared_elapsed_allowance_seconds=400,
            enforced_tool_call_limit=40,
            enforced_runtime_limit_seconds=900,
        )

    def test_submission_precedes_binding_and_sub_scrape_cycle_survives(self):
        observation = self.prepared()
        self.assertEqual(self.ledger.snapshot()["projects"][0]["dispatches"], [])
        observation.submitted(receipt_sha256=sha("submitted"))
        self.owner.drain_once()
        initial = self.ledger.snapshot()["projects"][0]["dispatches"][0]
        self.assertEqual(initial["observation"]["dispatch_id"], observation.dispatch_id)
        self.assertIsNone(initial["binding"])
        self.owner.observe_notification(
            {
                "method": "rawResponse/completed",
                "params": {
                    "threadId": "private-thread",
                    "turnId": "private-turn",
                    "responseId": "private-response",
                    "usage": None,
                    "privateReasoning": "not-allowed",
                },
            },
            sequence=1,
        )
        self.adapter.drain_once()
        self.assertEqual(
            self.ledger.snapshot()["projects"][0]["dispatches"][0]["aggregate"][
                "completed_cycles"
            ],
            0,
        )
        self.now = 101
        observation.acknowledged(
            thread_id="private-thread",
            turn_id="private-turn",
            receipt_sha256=sha("acknowledged"),
            configured_model="gpt-5.6-sol",
            configured_effort=None,
        )
        self.owner.drain_once()
        self.now = 102
        observation.running(receipt_sha256=sha("running"))
        self.owner.drain_once()
        self.now = 510
        observation.terminal(state="completed", receipt_sha256=sha("terminal"))
        self.owner.drain_once()
        snapshot = self.ledger.snapshot()
        dispatch = snapshot["projects"][0]["dispatches"][0]
        self.assertEqual(dispatch["aggregate"]["completed_cycles"], 1)
        self.assertEqual(dispatch["cycles"][0]["cycle_ordinal"], 1)
        self.assertIsNone(dispatch["cycles"][0]["usage"])
        self.assertEqual(dispatch["observation"]["timing"]["elapsed_seconds"], 410)
        payload = render_prometheus(build_samples(snapshot)[0]).decode()
        self.assertIn("cwo_cycle_present", payload)
        self.assertNotIn("\ncwo_cycle_tokens{", payload)
        self.assertNotIn("private-thread", json.dumps(snapshot))
        self.assertNotIn("not-allowed", json.dumps(snapshot))

    def test_preparation_and_invalid_receipt_never_invent_a_submission(self):
        observation = self.prepared()
        self.assertEqual(self.ledger.snapshot()["projects"][0]["dispatches"], [])
        self.assertEqual(self.owner._dispatch_ids, set())
        with self.assertRaisesRegex(ValueError, "controller receipt fingerprint"):
            observation.submitted(receipt_sha256="private-invalid-receipt")
        self.owner.drain_once()
        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(project["dispatches"], [])
        self.assertEqual(project["event_counts"]["lost"], 1)
        self.assertNotIn("private-invalid-receipt", json.dumps(project))

    def test_acknowledgement_without_submission_cannot_create_or_bind_dispatch(self):
        observation = self.prepared()
        observation.acknowledged(
            thread_id="private-thread",
            turn_id="private-turn",
            receipt_sha256=sha("ack"),
            configured_model="gpt-5.6-sol",
            configured_effort="xhigh",
        )
        self.owner.drain_once()
        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(project["dispatches"], [])
        self.assertEqual(project["event_counts"]["lost"], 1)

    def test_control_hook_uses_only_memory_queue_and_failure_is_isolated(self):
        observation = self.prepared()
        with mock.patch.object(
            self.ledger, "record_lifecycle", side_effect=RuntimeError("private failure")
        ) as writer:
            observation.submitted(receipt_sha256=sha("submitted"))
            writer.assert_not_called()
            self.owner.drain_once()
            writer.assert_called_once()
        snapshot = self.ledger.snapshot()
        self.assertEqual(
            snapshot["projects"][0]["dispatches"][0]["observation"]["lifecycle_state"],
            "submitted",
        )
        self.assertNotIn("private failure", json.dumps(snapshot))

    def test_close_drains_already_queued_terminal_receipt(self):
        observation = self.prepared()
        observation.submitted(receipt_sha256=sha("submitted"))
        self.now = 105
        observation.terminal(
            state="failed", receipt_sha256=sha("failed-before-running")
        )
        self.assertEqual(self.owner._queue.qsize(), 2)
        self.assertTrue(self.owner.close(timeout_seconds=1))
        self.assertTrue(self.owner._queue.empty())
        dispatch = self.ledger.snapshot()["projects"][0]["dispatches"][0]
        self.assertEqual(dispatch["observation"]["lifecycle_state"], "failed")
        self.assertEqual(dispatch["observation"]["timing"]["elapsed_seconds"], 5)

    def test_losses_count_each_cause_without_asserting_source_disconnect(self):
        observation = self.prepared()
        self.owner._queue = queue.Queue(maxsize=1)
        observation.submitted(receipt_sha256=sha("submitted"))
        observation.running(receipt_sha256=sha("running"))
        observation.terminal(state="completed", receipt_sha256=sha("completed"))
        with mock.patch.object(
            self.ledger, "record_lifecycle", side_effect=RuntimeError("private")
        ):
            self.owner.drain_once()
        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(project["event_counts"]["lost"], 3)
        self.assertEqual(project["health"]["reason_counts"]["queue-overflow"], 2)
        self.assertEqual(
            project["health"]["reason_counts"]["collector-write-failure"], 1
        )
        self.assertEqual(project["health"]["connection_state"], "connected")
        self.assertEqual(project["dispatches"][0]["aggregate"]["coverage_state"], 2)

    def test_hook_fault_count_does_not_disconnect_healthy_source(self):
        self.prepared()
        for _ in range(3):
            self.owner.collector_fault()
        self.owner.drain_once()
        project = self.ledger.snapshot()["projects"][0]
        self.assertEqual(
            project["health"]["reason_counts"]["collector-hook-failure"], 3
        )
        self.assertEqual(project["health"]["reason_counts"]["source-disconnected"], 0)
        self.assertEqual(project["health"]["connection_state"], "connected")




if __name__ == "__main__":
    unittest.main()
