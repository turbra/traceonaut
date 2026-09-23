from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_contract import CoverageState  # noqa: E402
from traceonaut.observability_host import (  # noqa: E402
    ObservabilityHostError,
    open_observability_host,
)
from traceonaut.observability_ledger import (  # noqa: E402
    DATABASE_NAME,
    ObservabilityLedger,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ObservabilityHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.credential = self.root / "metrics.token"
        self.credential.write_text("test-observability-token", encoding="ascii")
        self.credential.chmod(0o600)
        self.config_file = self.root / "observability.json"
        self.project_id = str(uuid.uuid4())
        self.registration = {
            "project_id": self.project_id,
            "owning_principal_id": os.geteuid(),
            "runtime_owner_id": str(uuid.uuid4()),
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

    @staticmethod
    def free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def config(self, *, port: int | None = None) -> dict[str, object]:
        return {
            "state_dir": str(self.state),
            "registration": self.registration,
            "executor_vocabulary": ["native_worker"],
            "model_vocabulary": ["qualified-model"],
            "effort_vocabulary": ["low", "xhigh"],
            "source_compatibility_sha256": sha("qualified-source"),
            "metrics": {
                "host": "127.0.0.1",
                "port": port or self.free_port(),
                "credential_file": str(self.credential),
            },
        }

    def write_config(self, value: object) -> None:
        self.config_file.write_text(json.dumps(value), encoding="utf-8")
        self.config_file.chmod(0o600)

    def test_opens_owned_stack_and_closes_connection_in_fifo_order(self) -> None:
        self.write_config(self.config())
        host = open_observability_host(self.config_file)
        dispatch = host.observer.prepare_dispatch(
            agent_id=str(uuid.uuid4()),
            packet_sha256=sha("packet"),
            supervisor_submission_ref="controller-submission-1",
            requested_executor="native_worker",
            requested_model="qualified-model",
            requested_effort="low",
            declared_cycle_allowance=None,
            declared_elapsed_allowance_seconds=None,
            enforced_tool_call_limit=10,
            enforced_runtime_limit_seconds=300,
        )
        dispatch.submitted(receipt_sha256=sha("submitted"))
        dispatch.acknowledged(
            thread_id="private-thread",
            turn_id="private-turn",
            receipt_sha256=sha("acknowledged"),
            configured_model="qualified-model",
            configured_effort="low",
        )
        for _ in range(50):
            rows = host.ledger.snapshot()["projects"][0]["dispatches"]
            if rows and rows[0]["binding"] is not None:
                current = rows[0]
                break
            time.sleep(0.01)
        else:
            self.fail("observability dispatch was not bound")

        self.assertIs(host.observer.adapter, host.adapter)
        self.assertEqual(
            current["aggregate"]["coverage_state"],
            int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        )
        self.assertEqual(host.metrics_endpoint.server.server_address[0], "127.0.0.1")
        self.assertTrue(host.close())

        snapshot = ObservabilityLedger.read_snapshot(self.state)
        project = snapshot["projects"][0]
        self.assertEqual(project["health"]["connection_state"], "disconnected")
        self.assertEqual(len(project["dispatches"]), 1)
        self.assertTrue(host.close())

    def test_invalid_config_is_rejected_before_state_or_listener_open(self) -> None:
        source = self.config()
        source["unexpected"] = "private-value"
        self.write_config(source)

        with self.assertRaisesRegex(
            ObservabilityHostError, "^observability-unavailable$"
        ):
            open_observability_host(self.config_file)

        self.assertFalse(self.state.exists())

    def test_config_must_be_owner_only_regular_file(self) -> None:
        self.write_config(self.config())
        self.config_file.chmod(0o644)

        with self.assertRaisesRegex(
            ObservabilityHostError, "^observability-unavailable$"
        ):
            open_observability_host(self.config_file)

        self.assertFalse(self.state.exists())

    def test_listener_failure_closes_threads_and_releases_writer(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = int(occupied.getsockname()[1])
            self.write_config(self.config(port=port))
            with self.assertRaisesRegex(
                ObservabilityHostError, "^observability-unavailable$"
            ):
                open_observability_host(self.config_file)

        with ObservabilityLedger(
            self.state,
            executor_vocabulary=("native_worker",),
            model_vocabulary=("qualified-model",),
            effort_vocabulary=("low", "xhigh"),
        ) as ledger:
            project = ledger.snapshot()["projects"][0]
            self.assertEqual(project["health"]["connection_state"], "disconnected")

    def test_bounded_incomplete_close_records_gap_and_can_be_retried(self) -> None:
        self.write_config(self.config())
        host = open_observability_host(self.config_file)
        observation = host.observer.prepare_dispatch(
            agent_id=str(uuid.uuid4()),
            packet_sha256=sha("packet"),
            supervisor_submission_ref="controller-submission-2",
            requested_executor="native_worker",
            requested_model="qualified-model",
            requested_effort="low",
        )
        observation.submitted(receipt_sha256=sha("close-gap-submitted"))
        for _ in range(50):
            if host.ledger.snapshot()["projects"][0]["dispatches"]:
                break
            time.sleep(0.01)
        else:
            self.fail("submitted observability dispatch was not persisted")
        with mock.patch.object(host.observer, "close", return_value=False):
            self.assertFalse(host.close(timeout_seconds=0.1))
            assert host._close_thread is not None
            host._close_thread.join(timeout=2)

        project = host.ledger.snapshot()["projects"][0]
        dispatch = project["dispatches"][0]
        self.assertEqual(
            dispatch["aggregate"]["coverage_state"], int(CoverageState.KNOWN_GAP)
        )
        self.assertIn(
            "shutdown-with-pending-events",
            project["health"]["known_coverage_gaps"],
        )
        self.assertEqual(
            project["health"]["reason_counts"]["shutdown-with-pending-events"],
            1,
        )
        self.assertEqual(
            dispatch["observation"]["dispatch_id"], observation.dispatch_id
        )
        self.assertTrue(host.close())

    def test_close_deadline_returns_while_safe_cleanup_continues(self) -> None:
        self.write_config(self.config())
        host = open_observability_host(self.config_file)
        observation = host.observer.prepare_dispatch(
            agent_id=str(uuid.uuid4()),
            packet_sha256=sha("deadline-packet"),
            supervisor_submission_ref="controller-deadline-submission",
            requested_executor="native_worker",
            requested_model="qualified-model",
            requested_effort="low",
        )
        observation.submitted(receipt_sha256=sha("deadline-submitted"))
        observation.acknowledged(
            thread_id="deadline-thread",
            turn_id="deadline-turn",
            receipt_sha256=sha("deadline-acknowledged"),
            configured_model="qualified-model",
            configured_effort="low",
        )
        observation.terminal(
            state="completed", receipt_sha256=sha("deadline-completed")
        )
        for _ in range(50):
            rows = host.ledger.snapshot()["projects"][0]["dispatches"]
            if (
                rows
                and rows[0]["binding"] is not None
                and rows[0]["observation"]["lifecycle_state"] == "completed"
            ):
                break
            time.sleep(0.01)
        else:
            self.fail("submitted observability dispatch was not persisted")
        original_close = host.export_service.close

        def delayed_close() -> None:
            time.sleep(0.25)
            original_close()

        started = time.monotonic()
        with mock.patch.object(host.export_service, "close", side_effect=delayed_close):
            self.assertFalse(host.close(timeout_seconds=0.01))
        self.assertLess(time.monotonic() - started, 0.15)
        project = host.ledger.snapshot()["projects"][0]
        self.assertEqual(project["health"]["connection_state"], "connected")
        time.sleep(0.35)
        self.assertTrue(host.close(timeout_seconds=1.0))
        closed = ObservabilityLedger.read_snapshot(self.state)["projects"][0]
        self.assertEqual(closed["health"]["connection_state"], "disconnected")
        self.assertEqual(
            closed["dispatches"][0]["aggregate"]["coverage_state"],
            int(CoverageState.OBSERVED_NO_KNOWN_GAP),
        )
        self.assertNotIn(
            "shutdown-with-pending-events",
            closed["health"]["known_coverage_gaps"],
        )

    def test_close_deadline_never_waits_for_ledger_mutex(self) -> None:
        self.write_config(self.config())
        host = open_observability_host(self.config_file)
        host.export_service.close()
        self.assertTrue(
            all(not thread.is_alive() for thread in host.export_service._threads)
        )
        host._service_closed = True
        locked = threading.Event()
        release = threading.Event()

        def hold_ledger() -> None:
            with host.ledger._lock:
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=hold_ledger, daemon=True)
        holder.start()
        self.assertTrue(locked.wait(1))
        started = time.monotonic()
        self.assertFalse(host.close(timeout_seconds=0.01))
        self.assertLess(time.monotonic() - started, 0.15)

        with closing(sqlite3.connect(self.state / DATABASE_NAME)) as reader:
            state = reader.execute("SELECT state FROM connections").fetchone()[0]
        self.assertEqual(state, "connected")

        release.set()
        holder.join(timeout=1)
        for _ in range(20):
            if host.close(timeout_seconds=0.25):
                break
        else:
            self.fail("observability host did not finish bounded cleanup")
        closed = ObservabilityLedger.read_snapshot(self.state)["projects"][0]
        self.assertEqual(closed["health"]["connection_state"], "disconnected")
        self.assertNotIn(
            "shutdown-with-pending-events",
            closed["health"]["known_coverage_gaps"],
        )


if __name__ == "__main__":
    unittest.main()
