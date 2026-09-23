"""Source accounting tests use synthetic metadata, never user transcripts."""

import copy
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.codex_session_telemetry import SessionCollector, render_session_metrics, write_snapshot
from collect_codex_sessions import SessionMetricsEndpoint


class SessionCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex"
        self.home.mkdir(mode=0o700)
        (self.home / "sessions").mkdir()
        self.state = self.root / "state"
        self.sid = str(uuid4())
        self.now = time.time() - 10
        self.path = self.home / "sessions" / ("rollout-2026-09-17-" + self.sid + ".jsonl")
        self.write(self.event("session_meta", {"id": self.sid, "cwd": "/workspace/example", "timestamp": self.now - 100, "base_instructions": "PRIVATE_INSTRUCTIONS"}, at=self.now - 100))
        self.collector = SessionCollector(self.home, self.state)
        self.addCleanup(lambda: self.collector.close())

    def event(self, kind, payload, at=None):
        at = self.now if at is None else at
        return {"timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(), "type": kind, "payload": payload}

    def write(self, *records, path=None):
        with (path or self.path).open("a") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")

    def usage(self, response="response-1", sid=None, total=30):
        return self.event("token_usage_record", {"thread_id": sid or self.sid, "response_id": response,
            "turn_id": "turn-1", "session_id": "connection", "root_turn_id": "root-turn",
            "usage": {"input_tokens": total - 10, "cached_input_tokens": 5, "cache_write_input_tokens": 0, "output_tokens": 10,
                      "reasoning_output_tokens": 2, "total_tokens": total},
            "thread_token_usage": {"total_tokens": 123456789}, "turn_token_usage": {"total_tokens": 98765}})

    def row(self):
        return next(r for r in self.collector.snapshot()["sessions"] if r["session_id"] == self.sid)

    def test_response_records_deduplicate_on_replay_restart_and_archive(self):
        record = self.usage()
        self.write(record, record)
        self.collector.scan()
        self.assertEqual(self.row()["usage"]["total"], 30)
        self.assertEqual(self.row()["responses"], 1)
        self.collector.close()
        self.collector = SessionCollector(self.home, self.state)
        archive = self.home / "archived_sessions"
        archive.mkdir()
        self.path.rename(archive / self.path.name)
        self.collector.scan()
        self.assertEqual(self.row()["usage"]["total"], 30)
        self.assertEqual(self.row()["responses"], 1)

    def test_copied_history_cannot_be_charged_to_child(self):
        self.write(self.usage(sid=str(uuid4())), self.usage("own-response"))
        self.collector.scan()
        self.assertEqual(self.row()["responses"], 1)
        self.assertEqual(self.row()["usage"]["total"], 30)

    def test_session_metadata_identity_wins_over_rollout_filename(self):
        self.path.rename(self.path.with_name("rollout-revert-" + str(uuid4()) + ".jsonl"))
        self.path = next((self.home / "sessions").iterdir())
        self.write(self.usage())
        self.collector.scan()
        self.assertEqual(self.row()["responses"], 1)

    def test_conflict_suppresses_numeric_usage(self):
        self.write(self.usage(), self.usage(total=40))
        self.collector.scan()
        row = self.row()
        self.assertEqual(row["usage_state"], 3)
        self.assertEqual(row["usage"], {})
        self.assertIsNone(row["responses"])

    def test_missing_or_invalid_usage_cannot_produce_a_complete_aggregate(self):
        record = self.usage()
        record["payload"]["usage"] = {"input_tokens": 0, "total_tokens": True}
        self.write(record)
        self.collector.scan()
        row = self.row()
        self.assertEqual(row["usage_state"], 3)
        self.assertEqual(row["usage"], {})
        metrics = render_session_metrics(self.collector.snapshot())
        self.assertNotIn(b'"output"', metrics)
        self.assertNotIn(b'"total"', metrics)

    def test_zero_usage_is_valid_when_present_in_complete_source_record(self):
        record = self.usage()
        record["payload"]["usage"] = {key: 0 for key in record["payload"]["usage"]}
        self.write(record)
        self.collector.scan()
        self.assertEqual(self.row()["usage"]["total"], 0)
        self.assertEqual(self.row()["usage_state"], 1)

    def test_unused_database_zero_is_unknown_until_a_numeric_record(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id,cwd,tokens_used)")
            db.execute("INSERT INTO threads VALUES(?,?,?)", (self.sid, "/workspace/example", 0))
            db.commit()
        self.collector.scan()
        self.assertIsNone(self.row()["reported_tokens"])
        self.write(self.event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 0}}}))
        self.collector.scan()
        self.assertEqual(self.row()["reported_tokens"], 0)

    def test_cache_write_or_identity_conflict_is_detected(self):
        for field in ("cache_write_input_tokens", "root_turn_id"):
            with self.subTest(field=field):
                record = self.usage(field)
                changed = copy.deepcopy(record)
                if field == "cache_write_input_tokens":
                    changed["payload"]["usage"][field] = 1
                else:
                    changed["payload"][field] = "different-root"
                self.write(record, changed)
                self.collector.scan()
                self.assertEqual(self.row()["usage_state"], 3)

    def test_replaced_file_reads_new_session_identity(self):
        self.collector.scan()
        new_sid = str(uuid4())
        replacement = self.path.with_suffix(".new")
        self.write(self.event("session_meta", {"id": new_sid, "cwd": "/workspace/new", "timestamp": self.now - 100}), self.usage(sid=new_sid), path=replacement)
        replacement.replace(self.path)
        self.collector.scan()
        row = next(r for r in self.collector.snapshot()["sessions"] if r["session_id"] == new_sid)
        self.assertEqual(row["responses"], 1)

    def test_invalid_newer_record_does_not_hide_valid_turn_state(self):
        self.write(self.event("event_msg", {"type": "task_started", "turn_id": []}, at=self.now + 2), self.event("event_msg", {"type": "task_started", "turn_id": "valid"}, at=self.now))
        self.collector.scan()
        self.assertEqual(self.row()["state"], 1)
        self.assertAlmostEqual(self.row()["last_event"], self.now, places=5)

    def test_collector_cannot_write_inside_source_home(self):
        with self.assertRaisesRegex(ValueError, "outside the Codex"):
            SessionCollector(self.home, self.home / "accounting")

    def test_malformed_candidate_cannot_hide_behind_other_valid_usage(self):
        for case in ("payload", "timestamp", "thread_id", "response_id", "identity"):
            with self.subTest(case=case):
                valid = self.usage("valid-" + case)
                invalid = self.usage("invalid-" + case)
                if case == "payload":
                    invalid["payload"] = None
                elif case == "timestamp":
                    invalid["timestamp"] = "invalid"
                elif case == "identity":
                    invalid["payload"]["session_id"] = []
                else:
                    invalid["payload"][case] = None
                with self.collector.db:
                    self.collector.db.execute("UPDATE sessions SET conflict=0")
                self.write(valid, invalid)
                self.collector.scan()
                self.assertEqual(self.row()["usage_state"], 3)
                self.assertEqual(self.row()["usage"], {})

    def test_invalid_json_usage_candidate_invalidates_existing_numeric_usage(self):
        self.write(self.usage())
        with self.path.open("a") as stream:
            stream.write('{"type":"token_usage_record","payload": BROKEN}\n')
        self.collector.scan()
        self.assertEqual(self.row()["usage_state"], 3)

    def test_oversized_usage_candidate_is_gated_and_reader_recovers(self):
        from unittest.mock import patch
        self.write(self.usage())
        self.collector.scan()
        with self.path.open("a") as stream:
            stream.write('{"type":"token_usage_record","payload":"' + 'x' * 1024 + '"}\n')
        with patch("traceonaut.codex_session_telemetry.MAX_LINE", 128):
            self.collector.scan()
        self.assertEqual(self.row()["usage_state"], 3)

    def test_legacy_cumulative_snapshot_is_never_summed_or_called_a_response(self):
        for total in (100, 100, 150):
            self.write(self.event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total}, "last_token_usage": {"total_tokens": 50}}}))
        self.collector.scan()
        row = self.row()
        self.assertEqual(row["reported_tokens"], 150)
        self.assertEqual(row["usage_state"], 2)
        self.assertEqual(row["usage"], {})
        self.assertIsNone(row["responses"])

    def test_turn_lifecycle_duration_and_no_recent_signal(self):
        self.write(self.event("event_msg", {"type": "task_started", "turn_id": "t1", "started_at": int(self.now)}))
        self.collector.scan()
        self.assertEqual(self.row()["state"], 1)
        self.assertEqual(self.collector.snapshot(now=self.now + 130)["sessions"][0]["state"], 4)
        complete = self.event("event_msg", {"type": "task_complete", "turn_id": "t1", "duration_ms": 2500, "last_agent_message": "PRIVATE_ANSWER"}, at=self.now + 1)
        self.write(complete, complete)
        self.collector.scan()
        row = self.row()
        self.assertEqual(row["state"], 2)
        self.assertEqual(row["completed_turns"], 1)
        self.assertEqual(row["observed_turn_seconds"], 2.5)

    def test_partial_line_is_retried_after_newline(self):
        raw = json.dumps(self.usage())
        with self.path.open("a") as f:
            f.write(raw[:50])
        self.collector.scan()
        self.assertIsNone(self.row()["responses"])
        with self.path.open("a") as f:
            f.write(raw[50:] + "\n")
        self.collector.scan()
        self.assertEqual(self.row()["responses"], 1)

    def test_rewrite_replays_without_recounting(self):
        self.write(self.usage())
        self.collector.scan()
        original = self.path.read_bytes()
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(original + (json.dumps(self.usage("response-2")) + "\n").encode())
        replacement.replace(self.path)
        self.collector.scan()
        self.assertEqual(self.row()["responses"], 2)
        self.assertEqual(self.row()["usage"]["total"], 60)

    def test_messages_tool_results_and_prompt_derived_titles_never_persist(self):
        dbpath = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute("CREATE TABLE threads(id,cwd,name,title,preview,first_user_message,tokens_used)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (self.sid, "/workspace/example", "Observability work", "PRIVATE_TITLE", "PRIVATE_PREVIEW", "PRIVATE_PROMPT", 55))
            db.commit()
        self.write(self.event("response_item", {"type": "message", "content": "PRIVATE_MESSAGE"}), self.event("response_item", {"type": "function_call_output", "output": "PRIVATE_TOOL_OUTPUT"}), self.usage())
        self.collector.scan()
        self.assertEqual(self.row()["title"], "Observability work")
        snapshot = self.collector.snapshot()
        write_snapshot(self.state / "snapshot.json", snapshot)
        for path in self.state.iterdir():
            self.assertNotIn(b"PRIVATE_", path.read_bytes(), path.name)
        self.assertNotIn(b"Observability work", render_session_metrics(snapshot))

    def test_symlink_source_is_not_read(self):
        outside = self.root / "outside.jsonl"
        self.path.rename(outside)
        self.path.symlink_to(outside)
        self.collector.scan()
        self.assertEqual(self.collector.snapshot()["sessions"], [])
        self.assertEqual(self.collector.snapshot()["errors"]["unsafe_file"], 1)

    def test_unavailable_source_is_distinct_from_no_events(self):
        self.collector.scan()
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot["source_available"], 1)
        self.assertEqual(snapshot["last_event"], 0)
        (self.home / "sessions").rename(self.home / "gone")
        with self.assertRaises(ValueError):
            self.collector.scan()
        self.assertFalse(self.collector.available)

    def test_metrics_remain_authenticated_and_include_actual_session_data(self):
        self.write(self.usage())
        self.collector.scan()
        token = b"fixture-not-a-real-credential"
        endpoint = SessionMetricsEndpoint("127.0.0.1", 0, token)
        self.addCleanup(endpoint.close)
        endpoint.update_sessions(self.collector.snapshot())
        endpoint.start()
        url = "http://127.0.0.1:" + str(endpoint.server.server_address[1]) + "/metrics"
        with self.assertRaises(HTTPError) as error:
            urlopen(url, timeout=2)
        self.assertEqual(error.exception.code, 401)
        with urlopen(Request(url, headers={"Authorization": "Bearer " + token.decode()}), timeout=2) as response:
            body = response.read()
        self.assertIn(b"cwo_codex_session_usage_tokens", body)
        self.assertNotIn(b"PRIVATE_", body)


if __name__ == "__main__":
    unittest.main()
