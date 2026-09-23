from __future__ import annotations

import hashlib
import io
import json
import copy
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid
from contextlib import redirect_stderr


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_contract import (  # noqa: E402
    CoverageState,
    FieldState,
    PublicationState,
    TelemetryDisposition,
)
from traceonaut.observability_ledger import (  # noqa: E402
    ObservabilityLedger,
    ObservabilityLedgerError,
)
from traceonaut.observability_terminal_export import (  # noqa: E402
    CURSOR_NAME,
    OBSERVATION_DIRECTORY_NAME,
    TerminalObservationExportError,
    TerminalObservationSink,
    export_terminal_observations,
)
import traceonaut.observability_terminal_export as terminal_export  # noqa: E402
from export_terminal_observations import main as cli_main  # noqa: E402


def identifier() -> str:
    return str(uuid.uuid4())


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class BarrierLedger(ObservabilityLedger):
    entered: threading.Event
    release: threading.Event

    def _snapshot_health(self, project_id: str) -> dict:
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("snapshot barrier timeout")
        return super()._snapshot_health(project_id)


class TerminalObservationExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.output = self.root / "output"
        self.project_id = identifier()
        self.runtime_owner_id = identifier()
        self.dispatch_id = identifier()
        self.agent_id = identifier()
        self.connection_id = identifier()
        self.binding_id = identifier()
        self.clock_epoch_id = identifier()
        self.compatibility = sha("qualified-source")
        self.sentinel = "PRIVATE_SENTINEL_DO_NOT_EXPORT"
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
                "max_pending_records": 64,
                "max_queue_records": 64,
                "pending_binding_deadline_seconds": 60,
                "max_registrations": 8,
                "max_disk_bytes": 32 * 1024 * 1024,
            },
        }

    def observation(self, *, dispatch_id: str | None = None) -> dict:
        return {
            "project_id": self.project_id,
            "dispatch_id": dispatch_id or self.dispatch_id,
            "agent_id": self.agent_id,
            "packet_ref": self.sentinel,
            "packet_sha256": sha(self.sentinel),
            "supervisor_submission_ref": "submission-7",
            "requested_executor": "native_worker",
            "requested_model": "qualified-model",
            "requested_effort": "xhigh",
            "declared_cycle_allowance": 14,
            "declared_elapsed_allowance_seconds": 400,
            "enforced_tool_call_limit": 20,
            "enforced_runtime_limit_seconds": 600,
            "lifecycle_state": "completed",
            "timing": {
                "clock_source": "supervisor-monotonic",
                "clock_epoch_id": self.clock_epoch_id,
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

    def establish_bound_terminal(self) -> None:
        self.ledger.record_submission(self.observation())
        self.ledger.register_connection(
            self.project_id, 1, self.connection_id, self.compatibility
        )
        self.thread = self.ledger.fingerprint_source_id(
            self.project_id, "thread", "raw-thread-" + self.sentinel
        )
        self.turn = self.ledger.fingerprint_source_id(
            self.project_id, "turn", "raw-turn-" + self.sentinel
        )
        self.ledger.bind_runtime(
            {
                "binding_id": self.binding_id,
                "project_id": self.project_id,
                "registration_generation": 1,
                "connection_id": self.connection_id,
                "dispatch_id": self.dispatch_id,
                "agent_id": self.agent_id,
                "packet_ref": self.sentinel,
                "packet_sha256": sha(self.sentinel),
                "supervisor_submission_ref": "submission-7",
                "thread_fingerprint": self.thread,
                "turn_fingerprint": self.turn,
                "acknowledgement_receipt_sha256": sha("ack"),
                "acknowledgement_provenance": "trusted-controller-receipt",
                "state": "active",
                "configured_model": "qualified-model",
                "configured_model_provenance": "acknowledged-thread-configuration",
                "configured_effort": "xhigh",
                "configured_effort_provenance": "acknowledged-thread-configuration",
            }
        )

    def completion(self, ordinal: int, usage: dict | None) -> dict:
        return {
            "project_id": self.project_id,
            "registration_generation": 1,
            "connection_id": self.connection_id,
            "thread_fingerprint": self.thread,
            "turn_fingerprint": self.turn,
            "event_fingerprint": sha(f"event-{ordinal}"),
            "observed_at_seconds": 200 + ordinal,
            "response_fingerprint": sha(f"response-{ordinal}"),
            "usage": usage,
            "purpose": "model-response",
            "runtime_emitted_at_seconds": None,
        }

    def close_source(self) -> None:
        self.ledger.mark_source_disconnected(
            self.project_id, 1, self.connection_id, expected=True
        )

    def observation_files(self) -> list[Path]:
        return sorted((self.output / OBSERVATION_DIRECTORY_NAME).glob("*.json"))

    def exported(self) -> dict:
        files = self.observation_files()
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_terminal_requires_closed_binding_disconnected_source_and_drained_queue(
        self,
    ) -> None:
        self.establish_bound_terminal()
        first = export_terminal_observations(self.state, self.output)
        self.assertEqual(first["eligible_runs"], 0)
        self.assertEqual(self.observation_files(), [])

        self.close_source()
        self.ledger.set_queue_depth(self.project_id, 1)
        second = export_terminal_observations(self.state, self.output)
        self.assertEqual(second["eligible_runs"], 0)

        self.ledger.set_queue_depth(self.project_id, 0)
        third = export_terminal_observations(self.state, self.output)
        self.assertEqual((third["eligible_runs"], third["upserted_runs"]), (1, 1))

    def test_partial_usage_is_not_zero_and_exact_allowance_arithmetic_is_retained(
        self,
    ) -> None:
        self.establish_bound_terminal()
        usage = {
            "input": 5,
            "cached_input": 0,
            "cache_write_input": 0,
            "output": 2,
            "reasoning_output": 0,
            "total": 7,
        }
        self.assertEqual(self.ledger.ingest_completion(self.completion(1, usage)), "accepted")
        self.assertEqual(self.ledger.ingest_completion(self.completion(2, None)), "accepted")
        for ordinal in range(3, 16):
            self.assertEqual(
                self.ledger.ingest_completion(self.completion(ordinal, usage)),
                "accepted",
            )
        self.close_source()

        export_terminal_observations(self.state, self.output)
        record = self.exported()
        input_tokens = record["accounting"]["tokens"]["input"]
        self.assertEqual(input_tokens["value"], 70)
        self.assertEqual(input_tokens["contributor_count"], 14)
        self.assertEqual(input_tokens["completed_cycles"], 15)
        self.assertEqual(input_tokens["state"], int(FieldState.PARTIAL))
        self.assertEqual(record["allowances"]["cycle"]["overrun"], 1)
        self.assertEqual(record["allowances"]["elapsed"]["overrun"], 10)
        self.assertEqual(record["allowances"]["enforced_tool_call_limit"], 20)
        self.assertEqual(record["allowances"]["enforced_runtime_limit_seconds"], 600)
        self.assertIsNone(record["configuration"]["actual"]["model"]["value"])
        self.assertEqual(
            record["configuration"]["actual"]["model"]["state"],
            int(FieldState.UNAVAILABLE),
        )

    def test_missing_usage_stays_unavailable_instead_of_becoming_zero(self) -> None:
        self.establish_bound_terminal()
        self.ledger.ingest_completion(self.completion(1, None))
        self.close_source()

        export_terminal_observations(self.state, self.output)
        token = self.exported()["accounting"]["tokens"]["input"]
        self.assertIsNone(token["value"])
        self.assertEqual(token["contributor_count"], 0)
        self.assertEqual(token["state"], int(FieldState.UNAVAILABLE))

    def test_health_only_correction_upserts_one_logical_run_and_replay_is_idempotent(
        self,
    ) -> None:
        self.establish_bound_terminal()
        self.close_source()
        first = export_terminal_observations(self.state, self.output)
        before = self.exported()

        self.ledger.set_publication_state(self.project_id, PublicationState.AVAILABLE)
        second = export_terminal_observations(self.state, self.output)
        after = self.exported()
        third = export_terminal_observations(self.state, self.output)

        self.assertEqual(first["upserted_runs"], 1)
        self.assertEqual(second["upserted_runs"], 1)
        self.assertEqual(third["eligible_runs"], 0)
        self.assertEqual(len(self.observation_files()), 1)
        self.assertEqual(
            before["revisions"]["accounting_revision"],
            after["revisions"]["accounting_revision"],
        )
        self.assertNotEqual(
            before["revisions"]["attributable_health_content_revision"],
            after["revisions"]["attributable_health_content_revision"],
        )
        sink = TerminalObservationSink(self.output)
        with self.assertRaisesRegex(
            TerminalObservationExportError, "source-revision-regressed"
        ):
            sink.upsert(before)
        self.assertEqual(self.exported(), after)

        same_revision_different_observation = copy.deepcopy(after)
        same_revision_different_observation["health"]["observation_time"][
            "ledger_bytes"
        ] += 1
        self.assertTrue(sink.upsert(same_revision_different_observation))
        observed = self.exported()
        self.assertEqual(
            observed["health"]["observation_time"]["ledger_bytes"],
            after["health"]["observation_time"]["ledger_bytes"] + 1,
        )
        self.assertFalse(sink.upsert(observed))

        same_revision_semantic_conflict = copy.deepcopy(observed)
        same_revision_semantic_conflict["publication"][
            "current_accounting_revision_confirmed"
        ] = not same_revision_semantic_conflict["publication"][
            "current_accounting_revision_confirmed"
        ]
        with self.assertRaisesRegex(
            TerminalObservationExportError, "equal-revision-conflict"
        ):
            sink.upsert(same_revision_semantic_conflict)
        self.assertEqual(self.exported(), observed)

    def test_later_shared_health_is_visible_without_rewriting_attributable_health(
        self,
    ) -> None:
        self.establish_bound_terminal()
        self.close_source()
        export_terminal_observations(self.state, self.output)
        before = self.exported()
        attributable = before["health"]["attributable"]

        self.ledger.record_submission(self.observation(dispatch_id=identifier()))
        self.ledger.record_health_event(
            self.project_id, TelemetryDisposition.UNASSIGNED
        )
        export_terminal_observations(self.state, self.output)
        after = self.exported()

        self.assertEqual(after["health"]["attributable"], attributable)
        self.assertEqual(
            after["health"]["current_attribution"]["state"],
            "unavailable-shared-or-unproven-scope",
        )
        self.assertEqual(
            after["health"]["latest_unattributed"]["snapshot"][
                "unassigned_count"
            ],
            1,
        )
        self.assertTrue(after["clean_summary_eligible"])
        self.assertEqual(
            after["revisions"]["attributable_health_content_revision"],
            attributable["content_revision"],
        )
        self.assertEqual(
            after["revisions"]["current_project_health_content_revision"],
            after["health"]["latest_unattributed"]["content_revision"],
        )

    def test_accounting_correction_under_shared_health_cannot_inherit_clean_status(
        self,
    ) -> None:
        self.establish_bound_terminal()
        self.close_source()
        export_terminal_observations(self.state, self.output)
        before = self.exported()
        old_attributable = before["health"]["attributable"]

        self.ledger.record_submission(self.observation(dispatch_id=identifier()))
        timing = dict(self.observation()["timing"])
        timing["terminal_seconds"] = 511
        timing["elapsed_seconds"] = 411
        self.ledger.record_lifecycle(
            self.dispatch_id,
            lifecycle_state="completed",
            timing=timing,
            receipt_sha256=sha("late-terminal-correction"),
        )
        export_terminal_observations(self.state, self.output)
        after = self.exported()

        self.assertGreater(
            after["revisions"]["accounting_revision"],
            before["revisions"]["accounting_revision"],
        )
        self.assertEqual(after["health"]["attributable"], old_attributable)
        self.assertFalse(after["clean_summary_eligible"])
        self.assertEqual(
            after["revisions"]["attributable_health_content_revision"],
            old_attributable["content_revision"],
        )
        self.ledger.record_health_event(
            self.project_id, TelemetryDisposition.DUPLICATE
        )
        export_terminal_observations(self.state, self.output)
        replayed = self.exported()
        self.assertFalse(replayed["clean_summary_eligible"])
        self.assertEqual(
            replayed["health"]["attributable"]["accounting_revision"],
            before["revisions"]["accounting_revision"],
        )

    def test_unprovable_shared_health_is_visible_and_excluded(self) -> None:
        second_id = identifier()
        self.ledger.record_submission(self.observation(dispatch_id=second_id))
        self.establish_bound_terminal()
        self.close_source()

        export_terminal_observations(self.state, self.output)
        record = self.exported()
        self.assertIsNone(record["health"]["attributable"])
        self.assertIsNotNone(record["health"]["latest_unattributed"])
        self.assertFalse(record["clean_summary_eligible"])

    def test_projection_excludes_private_content_and_sink_is_owner_private(self) -> None:
        self.establish_bound_terminal()
        self.close_source()
        export_terminal_observations(self.state, self.output)

        payload = b"".join(path.read_bytes() for path in self.output.rglob("*") if path.is_file())
        self.assertNotIn(self.sentinel.encode(), payload)
        for directory in (self.output, self.output / OBSERVATION_DIRECTORY_NAME):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(directory.stat().st_uid, os.geteuid())
        for path in (item for item in self.output.rglob("*") if item.is_file()):
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.geteuid())

    def test_unsafe_output_symlink_and_mode_are_rejected(self) -> None:
        bad = self.root / "bad"
        bad.mkdir(mode=0o755)
        with self.assertRaisesRegex(
            TerminalObservationExportError, "directory-unsafe"
        ):
            TerminalObservationSink(bad)
        link = self.root / "link"
        link.symlink_to(self.root)
        with self.assertRaisesRegex(
            TerminalObservationExportError, "directory-unsafe"
        ):
            TerminalObservationSink(link)

    def test_source_and_sink_overlap_is_rejected_before_any_source_mutation(self) -> None:
        before = {
            path.relative_to(self.state): path.read_bytes()
            for path in self.state.rglob("*")
            if path.is_file()
        }
        before_entries = sorted(path.relative_to(self.state) for path in self.state.rglob("*"))
        for output in (self.state, self.state / "nested-output"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(
                    TerminalObservationExportError, "source-sink-overlap"
                ):
                    export_terminal_observations(self.state, output)
        after = {
            path.relative_to(self.state): path.read_bytes()
            for path in self.state.rglob("*")
            if path.is_file()
        }
        after_entries = sorted(path.relative_to(self.state) for path in self.state.rglob("*"))
        self.assertEqual(after_entries, before_entries)
        self.assertEqual(after, before)

    def test_sink_rejects_extra_candidate_fields_at_every_projection_boundary(self) -> None:
        self.establish_bound_terminal()
        self.close_source()
        export_terminal_observations(self.state, self.output)
        record = self.exported()
        sink = TerminalObservationSink(self.output)

        top_level = copy.deepcopy(record)
        top_level["raw_content"] = self.sentinel
        with self.assertRaisesRegex(
            TerminalObservationExportError, "projection-record-invalid"
        ):
            sink.upsert(top_level)

        nested = copy.deepcopy(record)
        nested["configuration"]["requested"]["raw_prompt"] = self.sentinel
        with self.assertRaisesRegex(
            TerminalObservationExportError, "projection-requested-invalid"
        ):
            sink.upsert(nested)

    def test_sink_rejects_tampered_retained_attributable_health(self) -> None:
        self.establish_bound_terminal()
        self.close_source()
        export_terminal_observations(self.state, self.output)
        record = self.exported()
        path = self.observation_files()[0]
        tampered = copy.deepcopy(record)
        tampered["health"]["attributable"]["snapshot"]["raw_error"] = self.sentinel
        path.write_text(json.dumps(tampered) + "\n")
        sink = TerminalObservationSink(self.output)

        with self.assertRaisesRegex(
            TerminalObservationExportError, "health-snapshot-invalid"
        ):
            sink.upsert(record)

    def test_sink_rejects_forged_clean_revision_identity_and_attribution_links(self) -> None:
        self.establish_bound_terminal()
        self.close_source()
        export_terminal_observations(self.state, self.output)
        record = self.exported()
        sink = TerminalObservationSink(self.output)

        forged_clean = copy.deepcopy(record)
        forged_clean["health"]["attributable"]["clean"] = False
        forged_clean["clean_summary_eligible"] = False
        with self.assertRaisesRegex(
            TerminalObservationExportError, "health-clean-invalid"
        ):
            sink.upsert(forged_clean)

        forged_revision = copy.deepcopy(record)
        forged_revision["revisions"]["current_project_health_content_revision"] = sha(
            "unlinked"
        )
        with self.assertRaisesRegex(
            TerminalObservationExportError, "current-health-link-invalid"
        ):
            sink.upsert(forged_revision)

        forged_project = copy.deepcopy(record)
        other_project = identifier()
        captured = forged_project["health"]["attributable"]
        captured["snapshot"]["project_id"] = other_project
        captured["content_revision"] = terminal_export._content_revision(
            "cwo-terminal-health-content-v1", captured["snapshot"]
        )
        forged_project["revisions"]["attributable_health_content_revision"] = captured[
            "content_revision"
        ]
        forged_project["revisions"]["current_project_health_content_revision"] = captured[
            "content_revision"
        ]
        with self.assertRaisesRegex(
            TerminalObservationExportError, "current-health-link-invalid"
        ):
            sink.upsert(forged_project)

        forged_layout = copy.deepcopy(record)
        forged_layout["health"]["latest_unattributed"] = copy.deepcopy(
            forged_layout["health"]["attributable"]
        )
        with self.assertRaisesRegex(
            TerminalObservationExportError, "health-attribution-invalid"
        ):
            sink.upsert(forged_layout)

    def test_cursor_failure_replays_durable_upsert_after_restart(self) -> None:
        self.establish_bound_terminal()
        self.close_source()
        with mock.patch.object(
            TerminalObservationSink,
            "advance_cursor",
            side_effect=OSError("simulated cursor failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated cursor failure"):
                export_terminal_observations(self.state, self.output)
        self.assertEqual(len(self.observation_files()), 1)
        self.assertFalse((self.output / CURSOR_NAME).exists())

        result = export_terminal_observations(self.state, self.output)
        self.assertEqual(result["eligible_runs"], 1)
        self.assertTrue((self.output / CURSOR_NAME).exists())
        self.assertEqual(len(self.observation_files()), 1)

    def test_snapshot_read_transaction_is_consistent_across_writer_commit(self) -> None:
        self.ledger.record_submission(self.observation())
        old = self.ledger.snapshot()
        old_revision = old["snapshot_revision"]
        entered = threading.Event()
        release = threading.Event()
        reader = BarrierLedger(self.state, readonly=True)
        reader.entered = entered
        reader.release = release
        result: list[dict] = []
        failures: list[BaseException] = []

        def read() -> None:
            try:
                result.append(reader.snapshot())
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(entered.wait(5))
        self.ledger.set_publication_state(self.project_id, PublicationState.AVAILABLE)
        release.set()
        thread.join(5)
        reader.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result[0]["snapshot_revision"], old_revision)
        self.assertEqual(
            result[0]["projects"][0]["health"]["publication_state"],
            int(PublicationState.DISABLED),
        )
        fresh = self.ledger.snapshot()
        self.assertGreater(fresh["snapshot_revision"], old_revision)
        self.assertEqual(
            fresh["projects"][0]["health"]["publication_state"],
            int(PublicationState.AVAILABLE),
        )

    def test_snapshot_rejects_joining_an_uncommitted_writer_transaction(self) -> None:
        self.ledger._db.execute("BEGIN")
        try:
            with self.assertRaisesRegex(
                ObservabilityLedgerError, "snapshot-active-transaction"
            ):
                self.ledger.snapshot()
        finally:
            self.ledger._db.execute("ROLLBACK")

    def test_cli_exports_once_without_listener_or_optional_dependency(self) -> None:
        self.establish_bound_terminal()
        self.close_source()
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            status = cli_main(
                ["--state-dir", str(self.state), "--output-dir", str(self.output)]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["eligible_runs"], 1)

    def test_cli_missing_source_is_generic_and_does_not_disclose_path(self) -> None:
        missing = self.root / "private-missing-ledger"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = cli_main(
                ["--state-dir", str(missing), "--output-dir", str(self.output)]
            )
        self.assertEqual(status, 1)
        self.assertNotIn(str(missing), stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
