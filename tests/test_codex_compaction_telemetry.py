"""Focused tests for generic observed ContextCompaction telemetry."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import traceonaut.codex_session_telemetry as telemetry
from traceonaut.codex_session_telemetry import SessionCollector, render_session_metrics, write_snapshot


class CompactionTelemetryTests(unittest.TestCase):
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
            "cwd": "/workspace/compaction-telemetry",
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

    def compaction(
        self,
        item_id,
        *,
        turn_id="turn-1",
        owner=None,
        at=None,
        extra=None,
    ):
        item = {"type": "ContextCompaction", "id": item_id}
        item.update(extra or {})
        return self.event("event_msg", {
            "type": "item_completed",
            "thread_id": self.sid if owner is None else owner,
            "turn_id": turn_id,
            "item": item,
        }, at=at)

    def command(self, item_id="command-1"):
        return self.event("event_msg", {
            "type": "item_completed",
            "thread_id": self.sid,
            "turn_id": "turn-command",
            "item": {
                "type": "CommandExecution",
                "id": item_id,
                "status": "completed",
                "exit_code": 0,
                "duration": {"secs": 1, "nanos": 0},
            },
        })

    def usage(self):
        return self.event("token_usage_record", {
            "thread_id": self.sid,
            "response_id": "response-1",
            "turn_id": "turn-accounting",
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

    def state_record(self, *, now=None):
        return self.reliability(now=now)["compaction_telemetry"]

    def observations(self, *, now=None):
        return self.reliability(now=now)["compaction_observations"]

    def test_thirteen_qualified_shapes_are_observed_without_cause_claims(self):
        records = [self.compaction(f"context-{index}", turn_id=f"turn-{index}")
                   for index in range(13)]
        self.write(*records)
        self.collector.scan()

        observations = self.observations()
        self.assertEqual(len(observations), 13)
        self.assertEqual(self.state_record()["ready"], 1)
        self.assertTrue(all(len(row["observation_id"]) == 64 for row in observations))
        metrics = render_session_metrics(self.collector.snapshot()).decode()
        self.assertEqual(metrics.count("cwo_codex_compaction_observation_timestamp_seconds"), 13)
        for unsupported in ("manual_compaction", "automatic_compaction", "occupancy", "overflow", "percentage"):
            self.assertNotIn(unsupported, metrics)

    def test_privacy_and_non_compaction_items_are_excluded(self):
        self.write(
            self.compaction(
                "PRIVATE_COMPACTION_ITEM",
                turn_id="PRIVATE_COMPACTION_TURN",
                extra={
                    "prompt": "PRIVATE_PROMPT",
                    "output": "PRIVATE_OUTPUT",
                    "trigger": "PRIVATE_TRIGGER",
                },
            ),
            self.event("event_msg", {
                "type": "item_completed",
                "thread_id": self.sid,
                "turn_id": "other-turn",
                "item": {"type": "Reasoning", "id": "not-compaction"},
            }),
        )
        self.collector.scan()
        snapshot = self.collector.snapshot()
        write_snapshot(self.state / "snapshot.json", snapshot)
        metrics = render_session_metrics(snapshot)

        self.assertEqual(len(self.observations()), 1)
        sentinels = (
            b"PRIVATE_COMPACTION_ITEM", b"PRIVATE_COMPACTION_TURN",
            b"PRIVATE_PROMPT", b"PRIVATE_OUTPUT", b"PRIVATE_TRIGGER",
        )
        for path in self.state.iterdir():
            for sentinel in sentinels:
                self.assertNotIn(sentinel, path.read_bytes(), path.name)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, metrics)

    def test_owner_gap_is_compaction_specific_and_foreign_history_is_ignored(self):
        self.write(self.compaction("foreign", owner=str(uuid4())))
        self.collector.scan()
        self.assertEqual(self.observations(), [])
        self.assertEqual(self.state_record()["ready"], 1)

        missing_owner = self.compaction("missing-owner")
        missing_owner["payload"].pop("thread_id")
        self.write(missing_owner)
        self.collector.scan()
        self.assertEqual(self.state_record()["source_gaps"], 1)
        self.assertEqual(self.state_record()["ready"], 0)
        command_state = self.reliability()["command_telemetry"]
        self.assertEqual(command_state["source_gaps"], 0)
        self.assertEqual(command_state["ready"], 1)

    def test_command_defect_does_not_poison_compaction_coverage(self):
        command = self.command("missing-command-owner")
        command["payload"].pop("thread_id")
        self.write(command)
        self.collector.scan()
        reliability = self.reliability()
        self.assertEqual(reliability["command_telemetry"]["ready"], 0)
        self.assertEqual(reliability["compaction_telemetry"]["source_gaps"], 0)
        self.assertEqual(reliability["compaction_telemetry"]["ready"], 1)

    def test_identity_timestamp_and_pre_session_controls(self):
        before_session = self.compaction("copied", at=self.now - 200)
        invalid_turn = self.compaction("invalid-turn", turn_id="")
        invalid_item = self.compaction("", turn_id="turn-invalid-item")
        future = self.compaction("future", at=time.time() + 600)
        malformed = self.compaction("bad-time")
        malformed["timestamp"] = "invalid"
        self.write(before_session, invalid_turn, invalid_item, future, malformed)
        self.collector.scan()
        self.assertEqual(self.observations(), [])
        state = self.state_record()
        self.assertEqual(state["source_gaps"], 1)
        self.assertEqual(state["ready"], 0)

    def test_duplicate_restart_archive_and_conflict_semantics(self):
        record = self.compaction("stable")
        changed = self.compaction("stable", at=self.now + 1)
        self.write(record, record)
        self.collector.scan()
        accepted = self.observations()[0]
        self.assertEqual(len(self.observations()), 1)

        self._close()
        archive = self.home / "archived_sessions"
        archive.mkdir()
        archived = archive / self.path.name
        self.path.rename(archived)
        self.path = archived
        self.collector = SessionCollector(self.home, self.state)
        self.write(changed)
        self.collector.scan()

        row = self.observations()[0]
        self.assertEqual(row["observation_id"], accepted["observation_id"])
        self.assertEqual(row["conflict"], 1)
        self.assertAlmostEqual(row["timestamp"], self.now, places=5)
        self.assertEqual(self.state_record()["conflicts"], 1)
        self.assertEqual(self.state_record()["ready"], 0)

    def test_live_incremental_arrival_during_pending_historical_backfill(self):
        self.write(self.compaction("historical"))
        self.collector.scan(compaction_byte_budget=0)
        self.assertEqual(self.state_record()["pending_files"], 1)
        self.assertEqual(self.state_record()["ready"], 0)

        self.write(self.compaction("live", turn_id="turn-live", at=self.now + 1))
        self.collector.scan(compaction_byte_budget=0)
        self.assertEqual(len(self.observations()), 2)
        self.assertEqual(self.state_record()["pending_files"], 1)
        self.collector.scan()
        self.assertEqual(len(self.observations()), 2)
        self.assertEqual(self.state_record()["ready"], 1)

    def test_partial_line_retries_without_false_complete_state(self):
        raw = json.dumps(self.compaction("partial"))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(raw[:80])
        self.collector.scan()
        self.assertEqual(self.state_record()["pending_files"], 1)
        self.assertEqual(self.state_record()["ready"], 0)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(raw[80:] + "\n")
        self.collector.scan()
        self.assertEqual(len(self.observations()), 1)
        self.assertEqual(self.state_record()["ready"], 1)

    def test_removed_incomplete_source_is_compaction_gap_only(self):
        self.write(self.compaction("unscanned"))
        self.collector.scan(byte_budget=0, compaction_byte_budget=0)
        self.assertEqual(self.state_record()["pending_files"], 1)
        self.path.unlink()
        self.collector.scan()
        reliability = self.reliability()
        state = reliability["compaction_telemetry"]
        self.assertEqual(state["pending_files"], 0)
        self.assertEqual(state["source_gaps"], 1)
        self.assertEqual(state["ready"], 0)
        self.assertIsNone(state["complete_after_timestamp"])
        self.assertEqual(reliability["command_telemetry"]["source_gaps"], 0)
        self.assertEqual(reliability["command_telemetry"]["ready"], 1)

    def test_backfill_event_and_cursor_commit_atomically(self):
        self.write(self.compaction("atomic"))
        original = self.collector._consume

        def fail_after_insert(record, sid, **kwargs):
            original(record, sid, **kwargs)
            raise RuntimeError("synthetic compaction crash")

        self.collector._consume = fail_after_insert
        with self.assertRaisesRegex(RuntimeError, "synthetic compaction crash"):
            self.collector.scan(
                byte_budget=0,
                command_byte_budget=0,
            )
        self.assertEqual(
            self.collector.db.execute(
                "SELECT count(*) FROM compaction_observations"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.collector.db.execute("SELECT offset FROM compaction_files").fetchone()[0],
            0,
        )
        self.collector._consume = original
        self.collector.scan(byte_budget=0, command_byte_budget=0)
        self.assertEqual(len(self.observations()), 1)

    def test_oversized_item_envelope_is_a_gap_not_a_covered_zero(self):
        self.write(self.compaction("oversized", extra={"opaque": "x" * 1024}))
        with patch.object(telemetry, "MAX_LINE", 128):
            self.collector.scan()
        state = self.state_record()
        self.assertEqual(state["source_gaps"], 1)
        self.assertEqual(state["ready"], 0)
        self.assertIsNone(state["complete_after_timestamp"])

    def test_cap_ties_use_deterministic_exclusive_watermark(self):
        tied_at = self.now - 1
        self.write(*(self.compaction(f"tie-{index}", turn_id=f"turn-{index}", at=tied_at)
                     for index in range(3)))
        self.collector.compaction_export_cap = 2
        self.collector.scan()
        first = self.reliability(now=self.now)
        second = self.reliability(now=self.now)
        state = first["compaction_telemetry"]
        self.assertEqual(state["cap"], 2)
        self.assertEqual(state["cap_truncated"], 1)
        self.assertEqual(state["observations"], 2)
        self.assertAlmostEqual(state["complete_after_timestamp"], tied_at, places=5)
        first_ids = [row["observation_id"] for row in first["compaction_observations"]]
        second_ids = [row["observation_id"] for row in second["compaction_observations"]]
        self.assertEqual(first_ids, second_ids)

    def test_compaction_options_are_strict_bounded_and_default_to_sixty_four(self):
        self.assertEqual(self.collector.compaction_export_cap, 64)
        self._close()
        for value in (0, -1, True, 1.5, 513):
            with self.subTest(cap=value), self.assertRaises(ValueError):
                SessionCollector(self.home, self.state, compaction_export_cap=value)
        for value in (0, -1, True, 1.5, telemetry.COMMAND_RETENTION_SECONDS + 1):
            with self.subTest(retention=value), self.assertRaises(ValueError):
                SessionCollector(
                    self.home, self.state, compaction_retention_seconds=value
                )
        self.collector = SessionCollector(
            self.home,
            self.state,
            compaction_export_cap=2,
            compaction_retention_seconds=60,
        )
        self.assertEqual(self.collector.compaction_export_cap, 2)
        self.assertEqual(self.collector.compaction_retention_seconds, 60)

    def test_d_migration_preserves_c_cursors_observations_and_accounting(self):
        self.write(
            self.command(),
            self.compaction("historical-compaction"),
            self.usage(),
            self.event("event_msg", {
                "type": "task_complete",
                "turn_id": "turn-accounting",
                "duration_ms": 1000,
            }, at=self.now + 1),
        )
        self.collector.scan()
        before = {
            "command_count": self.collector.db.execute(
                "SELECT count(*) FROM command_observations"
            ).fetchone()[0],
            "command_offset": self.collector.db.execute(
                "SELECT offset FROM command_files"
            ).fetchone()[0],
            "file_offset": self.collector.db.execute(
                "SELECT offset FROM files"
            ).fetchone()[0],
            "responses": self.collector.snapshot()["sessions"][0]["responses"],
            "turns": self.collector.snapshot()["sessions"][0]["completed_turns"],
        }
        with self.collector.db:
            self.collector.db.execute("DROP TABLE compaction_observations")
            self.collector.db.execute("DROP TABLE compaction_files")
            self.collector.db.execute(
                "DELETE FROM reliability_features WHERE name='compaction_telemetry'"
            )
        self._close()

        self.collector = SessionCollector(self.home, self.state)
        self.assertEqual(
            self.collector.db.execute("SELECT count(*) FROM command_observations").fetchone()[0],
            before["command_count"],
        )
        self.assertEqual(
            self.collector.db.execute("SELECT offset FROM command_files").fetchone()[0],
            before["command_offset"],
        )
        self.assertEqual(
            self.collector.db.execute("SELECT offset FROM files").fetchone()[0],
            before["file_offset"],
        )
        self.collector.scan()
        session = self.collector.snapshot()["sessions"][0]
        self.assertEqual(session["responses"], before["responses"])
        self.assertEqual(session["completed_turns"], before["turns"])
        self.assertEqual(len(self.observations()), 1)
        self.assertEqual(self.state_record()["ready"], 1)


if __name__ == "__main__":
    unittest.main()
