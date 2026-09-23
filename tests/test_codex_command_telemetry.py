"""Focused tests for content-free Codex command-event telemetry."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import traceonaut.codex_session_telemetry as telemetry
from traceonaut.codex_session_telemetry import SessionCollector, render_session_metrics, write_snapshot


class CommandTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex"
        self.home.mkdir(mode=0o700)
        (self.home / "sessions").mkdir()
        self.state = self.root / "state"
        self.sid = str(uuid4())
        self.now = round(time.time() - 10, 6)
        self.path = self.home / "sessions" / f"rollout-{self.sid}.jsonl"
        self.write(self.event("session_meta", {
            "id": self.sid,
            "cwd": "/workspace/command-telemetry",
            "timestamp": self.now - 100,
        }, at=self.now - 100))
        self.collector = SessionCollector(self.home, self.state)
        self.addCleanup(self._close)

    def _close(self):
        if self.collector is not None:
            self.collector.close()
            self.collector = None

    def event(self, kind, payload, *, at=None):
        at = self.now if at is None else at
        return {
            "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
            "type": kind,
            "payload": payload,
        }

    def command(
        self,
        item_id,
        *,
        turn_id="turn-1",
        owner=None,
        status="completed",
        exit_code=0,
        duration=(0, 0),
        at=None,
        sensitive=False,
    ):
        item = {
            "type": "CommandExecution",
            "id": item_id,
            "status": status,
            "exit_code": exit_code,
        }
        if duration is not None:
            item["duration"] = {"secs": duration[0], "nanos": duration[1]}
        if sensitive:
            item.update({
                "command": "PRIVATE_COMMAND_SENTINEL",
                "stdout": "PRIVATE_STDOUT_SENTINEL",
                "stderr": "PRIVATE_STDERR_SENTINEL",
                "output": "PRIVATE_OUTPUT_SENTINEL",
            })
        return self.event("event_msg", {
            "type": "item_completed",
            "thread_id": self.sid if owner is None else owner,
            "turn_id": turn_id,
            "item": item,
        }, at=at)

    def usage(self):
        return self.event("token_usage_record", {
            "thread_id": self.sid,
            "response_id": "response-1",
            "turn_id": "turn-1",
            "session_id": "connection",
            "root_turn_id": "root-turn",
            "usage": {
                "input_tokens": 20,
                "cached_input_tokens": 5,
                "cache_write_input_tokens": 0,
                "output_tokens": 10,
                "reasoning_output_tokens": 2,
                "total_tokens": 30,
            },
        })

    def write(self, *records, path=None):
        with (path or self.path).open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    def reliability(self, *, now=None):
        return self.collector.snapshot(now=now)["reliability"]

    def commands(self, *, now=None):
        return self.reliability(now=now)["command_observations"]

    def test_valid_outcomes_zero_and_fractional_runtime_durations(self):
        self.write(
            self.command("zero", duration=(0, 0)),
            self.command("failed", status="failed", exit_code=7, duration=(2, 500_000_000)),
        )
        self.collector.scan()

        commands = {row["outcome"]: row for row in self.commands()}
        self.assertEqual(commands["completed"]["duration_seconds"], 0)
        self.assertEqual(commands["failed"]["duration_seconds"], 2.5)
        metrics = render_session_metrics(self.collector.snapshot()).decode()
        self.assertIn("cwo_codex_command_event_timestamp_seconds", metrics)
        self.assertIn("cwo_codex_command_event_duration_seconds", metrics)
        self.assertNotIn("command_observation_failed", metrics)
        self.assertNotIn("command_observation_metadata_state", metrics)

    def test_unknown_outcome_keeps_valid_duration_and_invalid_duration_is_absent(self):
        records = [
            self.command("missing-status", status=None, duration=(1, 250_000_000)),
            self.command("bool-exit", exit_code=True),
            self.command("mismatch", status="completed", exit_code=1),
            self.command("negative-exit", status="failed", exit_code=-9),
            self.command("negative", duration=(-1, 0)),
            self.command("large-nanos", duration=(0, 1_000_000_000)),
            self.command("bool-seconds", duration=(True, 0)),
            self.command("rounded-overflow", duration=(telemetry.SAFE_INTEGER, 1)),
            self.command("missing-duration", duration=None),
        ]
        self.write(*records)
        self.collector.scan()

        rows = self.commands()
        self.assertEqual(len(rows), len(records))
        self.assertEqual({row["outcome"] for row in rows}, {"completed", "unknown"})
        durations = sorted(
            row["duration_seconds"] for row in rows if row["duration_seconds"] is not None
        )
        self.assertEqual(durations, [0.0, 0.0, 0.0, 1.25])
        self.assertEqual(self.reliability()["command_telemetry"]["ready"], 1)

    def test_sensitive_fields_and_raw_identities_never_persist_or_export(self):
        self.write(self.command(
            "PRIVATE_ITEM_ID_SENTINEL",
            turn_id="PRIVATE_TURN_ID_SENTINEL",
            sensitive=True,
        ))
        self.collector.scan()
        snapshot = self.collector.snapshot()
        write_snapshot(self.state / "snapshot.json", snapshot)
        metrics = render_session_metrics(snapshot)

        sentinels = (
            b"PRIVATE_COMMAND_SENTINEL", b"PRIVATE_STDOUT_SENTINEL",
            b"PRIVATE_STDERR_SENTINEL", b"PRIVATE_OUTPUT_SENTINEL",
            b"PRIVATE_ITEM_ID_SENTINEL", b"PRIVATE_TURN_ID_SENTINEL",
        )
        for path in self.state.iterdir():
            data = path.read_bytes()
            for sentinel in sentinels:
                self.assertNotIn(sentinel, data, path.name)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, metrics)
        observation_id = self.commands()[0]["observation_id"]
        self.assertEqual(len(observation_id), 64)
        int(observation_id, 16)

    def test_duplicate_restart_and_archive_replay_are_idempotent(self):
        command = self.command("same")
        self.write(command, command)
        self.collector.scan()
        self.assertEqual(len(self.commands()), 1)

        self._close()
        archive = self.home / "archived_sessions"
        archive.mkdir()
        self.path = archive / self.path.name
        (self.home / "sessions" / self.path.name).rename(self.path)
        self.collector = SessionCollector(self.home, self.state)
        self.collector.scan()
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(self.reliability()["command_telemetry"]["conflicts"], 0)

    def test_feature_migration_never_rewinds_usage_cursor_or_double_counts_tokens(self):
        self.write(self.usage(), self.command("historical"))
        self.collector.scan()
        file_offset = self.collector.db.execute("SELECT offset FROM files").fetchone()[0]
        self.assertEqual(self.collector.snapshot()["sessions"][0]["responses"], 1)

        with self.collector.db:
            self.collector.db.execute("DELETE FROM command_observations")
            self.collector.db.execute(
                "UPDATE reliability_features SET version=0,backfill_complete=0"
            )
        self._close()
        self.collector = SessionCollector(self.home, self.state)
        self.assertEqual(
            self.collector.db.execute("SELECT offset FROM files").fetchone()[0],
            file_offset,
        )
        self.assertEqual(
            self.collector.db.execute("SELECT offset FROM command_files").fetchone()[0],
            0,
        )
        self.collector.scan()
        row = self.collector.snapshot()["sessions"][0]
        self.assertEqual(row["responses"], 1)
        self.assertEqual(row["usage"]["total"], 30)
        self.assertEqual(len(self.commands()), 1)

    def test_wrong_owner_is_ignored_but_missing_owner_blocks_readiness(self):
        self.write(self.command("foreign", owner=str(uuid4())))
        self.collector.scan()
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.reliability()["command_telemetry"]["ready"], 1)

        missing = self.command("missing-owner")
        missing["payload"].pop("thread_id")
        self.write(missing)
        self.collector.scan()
        telemetry_state = self.reliability()["command_telemetry"]
        self.assertEqual(telemetry_state["source_gaps"], 1)
        self.assertEqual(telemetry_state["ready"], 0)
        self.assertIsNone(telemetry_state["complete_after_timestamp"])

    def test_invalid_identity_and_future_timestamp_are_unrepresentable_gaps(self):
        invalid_id = self.command("item", turn_id="")
        future = self.command("future", at=time.time() + 600)
        malformed = self.command("malformed-time")
        malformed["timestamp"] = "not-a-timestamp"
        self.write(invalid_id, future, malformed)
        self.collector.scan()
        state = self.reliability()["command_telemetry"]
        self.assertEqual(self.commands(), [])
        self.assertEqual(state["source_gaps"], 1)
        self.assertEqual(state["ready"], 0)

    def test_same_owner_history_before_session_creation_is_ignored_as_fork_history(self):
        self.write(self.command("historical", at=self.now - 200))
        self.collector.scan()
        state = self.reliability()["command_telemetry"]
        self.assertEqual(self.commands(), [])
        self.assertEqual(state["source_gaps"], 0)
        self.assertEqual(state["ready"], 1)

    def test_timestamp_at_session_creation_boundary_is_accepted(self):
        self.write(self.command("earliest-owned", at=self.now - 100))
        self.collector.scan()
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(self.reliability()["command_telemetry"]["ready"], 1)

    def test_conflicting_payload_is_quarantined_without_overwrite(self):
        self.write(
            self.command("conflict", duration=(1, 0)),
            self.command("conflict", duration=(2, 0)),
        )
        self.collector.scan()
        row = self.commands()[0]
        state = self.reliability()["command_telemetry"]
        self.assertEqual(row["outcome"], "conflict")
        self.assertIsNone(row["duration_seconds"])
        self.assertEqual(state["conflicts"], 1)
        self.assertEqual(state["ready"], 0)
        accepted = self.collector.db.execute(
            "SELECT outcome,duration,conflict FROM command_observations"
        ).fetchone()
        self.assertEqual(tuple(accepted), ("completed", 1.0, 1))
        metrics = render_session_metrics(self.collector.snapshot()).decode()
        self.assertIn('outcome="conflict"', metrics)
        conflict_id = row["observation_id"]
        duration_lines = [line for line in metrics.splitlines()
                          if "command_event_duration" in line and conflict_id in line]
        self.assertEqual(duration_lines, [])

    def test_partial_line_and_pending_backfill_never_claim_complete_coverage(self):
        raw = json.dumps(self.command("partial"))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(raw[:60])
        self.collector.scan()
        state = self.reliability()["command_telemetry"]
        self.assertEqual(state["pending_files"], 1)
        self.assertEqual(state["backfill_complete"], 0)
        self.assertIsNone(state["complete_after_timestamp"])

        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(raw[60:] + "\n")
        self.collector.scan(command_byte_budget=0)
        state = self.reliability()["command_telemetry"]
        self.assertEqual(len(self.commands()), 1)  # live parser observed it first
        self.assertEqual(state["pending_files"], 1)
        self.assertEqual(state["ready"], 0)
        self.collector.scan()
        self.assertEqual(self.reliability()["command_telemetry"]["ready"], 1)

    def test_removed_incomplete_source_is_a_durable_coverage_gap(self):
        self.write(self.command("unscanned"))
        self.collector.scan(byte_budget=0, command_byte_budget=0)
        before = self.reliability()["command_telemetry"]
        self.assertEqual(before["pending_files"], 1)
        self.assertEqual(before["ready"], 0)

        self.path.unlink()
        self.collector.scan()
        after = self.reliability()["command_telemetry"]
        self.assertEqual(after["pending_files"], 0)
        self.assertEqual(after["backfill_complete"], 1)
        self.assertEqual(after["source_gaps"], 1)
        self.assertEqual(after["ready"], 0)
        self.assertIsNone(after["complete_after_timestamp"])

    def test_replaced_incomplete_source_is_a_durable_coverage_gap(self):
        self.write(self.command("lost-before-replacement"))
        self.collector.scan(byte_budget=0, command_byte_budget=0)
        replacement = self.path.with_suffix(".replacement")
        self.write(
            self.event("session_meta", {
                "id": self.sid,
                "cwd": "/workspace/command-telemetry",
                "timestamp": self.now - 100,
            }, at=self.now - 100),
            self.command("replacement-command"),
            path=replacement,
        )
        replacement.replace(self.path)

        self.collector.scan()
        state = self.reliability()["command_telemetry"]
        self.assertEqual(state["source_gaps"], 1)
        self.assertEqual(state["ready"], 0)
        self.assertEqual(len(self.commands()), 1)

    def test_incomplete_source_cursor_follows_archive_rename(self):
        self.write(self.command("renamed-before-backfill"))
        self.collector.scan(byte_budget=0, command_byte_budget=0)
        archive = self.home / "archived_sessions"
        archive.mkdir()
        archived_path = archive / self.path.name
        self.path.rename(archived_path)
        self.path = archived_path

        self.collector.scan(byte_budget=0, command_byte_budget=0)
        pending = self.reliability()["command_telemetry"]
        command_file = self.collector.db.execute(
            "SELECT path,offset,complete FROM command_files"
        ).fetchone()
        self.assertTrue(command_file["path"].startswith("archived_sessions/"))
        self.assertEqual(tuple(command_file)[1:], (0, 0))
        self.assertEqual(pending["source_gaps"], 0)
        self.assertEqual(pending["pending_files"], 1)

        self.collector.scan()
        complete = self.reliability()["command_telemetry"]
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(complete["source_gaps"], 0)
        self.assertEqual(complete["ready"], 1)

    def test_backfill_event_and_cursor_commit_atomically(self):
        self.write(self.command("atomic"))
        original = self.collector._consume

        def fail_after_insert(record, sid, **kwargs):
            original(record, sid, **kwargs)
            raise RuntimeError("synthetic crash")

        self.collector._consume = fail_after_insert
        with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
            self.collector.scan(byte_budget=0)
        self.assertEqual(
            self.collector.db.execute("SELECT count(*) FROM command_observations").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.collector.db.execute("SELECT offset FROM command_files").fetchone()[0],
            0,
        )
        self.collector._consume = original
        self.collector.scan(byte_budget=0)
        self.assertEqual(len(self.commands()), 1)

    def test_historical_parser_finds_item_type_after_four_kibibytes(self):
        record = self.command("late-type")
        item = record["payload"]["item"]
        record["payload"]["item"] = {"command": "x" * 5000, **item}
        raw = json.dumps(record)
        self.assertGreater(raw.index('"CommandExecution"'), 4096)
        self.write(record)

        self.collector.scan(byte_budget=0)
        self.assertEqual(len(self.commands()), 1)
        state = self.reliability()["command_telemetry"]
        self.assertEqual(state["source_gaps"], 0)
        self.assertEqual(state["ready"], 1)

    def test_cap_selection_is_deterministic_and_watermark_is_exclusive_for_ties(self):
        tied_at = self.now - 1
        records = [self.command(f"tie-{index}", at=tied_at) for index in range(3)]
        self.write(*records)
        self.collector.command_export_cap = 2
        self.collector.scan()
        first = self.reliability(now=self.now)
        second = self.reliability(now=self.now)
        state = first["command_telemetry"]
        self.assertEqual(state["cap_truncated"], 1)
        self.assertEqual(state["observations"], 2)
        self.assertAlmostEqual(state["complete_after_timestamp"], tied_at, places=5)
        first_ids = [row["observation_id"] for row in first["command_observations"]]
        second_ids = [row["observation_id"] for row in second["command_observations"]]
        self.assertEqual(first_ids, second_ids)

        metrics = render_session_metrics(self.collector.snapshot(now=self.now)).decode()
        timestamp_ids = {
            line.split('observation_id="', 1)[1].split('"', 1)[0]
            for line in metrics.splitlines() if "command_event_timestamp" in line
        }
        duration_ids = {
            line.split('observation_id="', 1)[1].split('"', 1)[0]
            for line in metrics.splitlines() if "command_event_duration" in line
        }
        self.assertEqual(timestamp_ids, duration_ids)
        self.assertEqual(timestamp_ids, set(first_ids))

    def test_export_options_are_strict_positive_bounded_integers(self):
        self._close()
        bad_values = (0, -1, True, 1.5, 10_001)
        for value in bad_values:
            with self.subTest(cap=value), self.assertRaises(ValueError):
                SessionCollector(self.home, self.state, command_export_cap=value)
        for value in (0, -1, True, 1.5, telemetry.COMMAND_RETENTION_SECONDS + 1):
            with self.subTest(retention=value), self.assertRaises(ValueError):
                SessionCollector(self.home, self.state, command_retention_seconds=value)
        self.collector = SessionCollector(
            self.home,
            self.state,
            command_export_cap=2,
            command_retention_seconds=60,
        )
        self.assertEqual(self.collector.command_export_cap, 2)
        self.assertEqual(self.collector.command_retention_seconds, 60)

    def test_retention_floor_and_snapshot_time_are_scan_bound(self):
        boundary = self.now - telemetry.COMMAND_RETENTION_SECONDS
        self.collector.sync_inventory()
        with self.collector.db:
            self.collector.db.execute(
                "UPDATE sessions SET created=? WHERE id=?", (boundary - 100, self.sid)
            )
        self.write(
            self.command("boundary", at=boundary),
            self.command("expired", at=boundary - 0.001),
        )
        self.collector.scan()
        first = self.reliability(now=self.now)
        state = first["command_telemetry"]
        self.assertEqual(len(first["command_observations"]), 1)
        self.assertEqual(state["complete_after_timestamp"], boundary)
        scan_timestamp = state["snapshot_timestamp"]
        later = self.reliability(now=self.now + 30)["command_telemetry"]
        self.assertEqual(later["snapshot_timestamp"], scan_timestamp)


if __name__ == "__main__":
    unittest.main()
