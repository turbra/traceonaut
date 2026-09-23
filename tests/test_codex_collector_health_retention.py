"""Synthetic collector health and exposition-only retention contracts."""
from __future__ import annotations

import copy
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
from traceonaut.codex_session_telemetry import (
    DEFAULT_SESSION_EXPORT_CAP, SESSION_RETENTION_SECONDS, SKIP_REASONS,
    SessionCollector, render_session_metrics, session_export_rows,
)
from render_codex_sessions_dashboard import validate_snapshot


class SessionExportSelectionTests(unittest.TestCase):
    def test_cutoff_inclusive_created_fallback_unknown_and_future(self):
        rows = [dict(session_id=sid, last_event=at, created=created) for sid, at, created in (
            ("old", 899, 0), ("boundary", 900, 0), ("new", 950, 0),
            ("created", 0, 975), ("unknown", 0, 0), ("future", 1100, 0),
        )]
        before = copy.deepcopy(rows)
        retained, policy = session_export_rows(rows, now=1000, retention_seconds=100, cap=10)
        self.assertEqual([r["session_id"] for r in retained], ["future", "created", "new", "boundary", "unknown"])
        self.assertEqual(policy["expired_sessions"], 1)
        self.assertEqual(policy["exported_sessions"], 5)
        self.assertEqual(policy["cap_omitted_sessions"], 0)
        self.assertEqual(rows, before)

    def test_cap_is_deterministic_and_zero_disables_only_age(self):
        rows = [dict(session_id=sid, last_event=at) for sid, at in (("c", 2), ("b", 2), ("a", 1))]
        retained, policy = session_export_rows(rows, now=1000, retention_seconds=0, cap=2)
        self.assertEqual([r["session_id"] for r in retained], ["b", "c"])
        self.assertEqual(policy["expired_sessions"], 0)
        self.assertEqual(policy["cap_omitted_sessions"], 1)
        self.assertEqual(policy["cap_truncated"], 1)
        self.assertEqual(session_export_rows(list(reversed(rows)), now=1000, retention_seconds=0, cap=2)[0], retained)
        self.assertEqual(session_export_rows([], now=1000, retention_seconds=10, cap=1)[1]["exported_sessions"], 0)


class CollectorHealthRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "source"
        (self.home / "sessions").mkdir(parents=True, mode=0o700)
        self.home.chmod(0o700)
        self.state = Path(self.tmp.name) / "index"
        self.sid = str(uuid4())
        self.now = round(time.time() - 10, 6)
        self.path = self.home / "sessions" / "rollout-fixture.jsonl"
        self.append(self.event("session_meta", {"id": self.sid, "cwd": "/example", "timestamp": self.now - 100}))
        self.collector = SessionCollector(self.home, self.state, session_retention_seconds=60, session_export_cap=1)
        self.addCleanup(lambda: self.collector.close())

    def event(self, kind, payload, *, at=None):
        return dict(timestamp=datetime.fromtimestamp(self.now if at is None else at, timezone.utc).isoformat(), type=kind, payload=payload)

    def append(self, *records):
        with self.path.open("a") as out:
            for record in records:
                out.write(json.dumps(record) + "\n")

    def usage(self, response):
        return self.event("token_usage_record", dict(thread_id=self.sid, response_id=response,
            turn_id="turn", session_id="connection", root_turn_id="root",
            usage=dict(input_tokens=20, cached_input_tokens=5, cache_write_input_tokens=0,
                       output_tokens=10, reasoning_output_tokens=2, total_tokens=30)))

    def counters(self):
        return self.collector.snapshot()["skipped_records"]

    def test_expiry_preserves_index_snapshot_names_and_resume_accounting(self):
        self.append(self.usage("one"))
        source = self.path.read_bytes()
        self.collector.scan()
        at_boundary = self.collector.snapshot(now=self.now + 60)
        self.assertIn(f'session_id="{self.sid}"'.encode(), render_session_metrics(at_boundary))
        expired = self.collector.snapshot(now=self.now + 61)
        self.assertEqual(expired["session_export"]["expired_sessions"], 1)
        self.assertNotIn(f'session_id="{self.sid}"'.encode(), render_session_metrics(expired))
        self.assertEqual(expired["sessions"][0]["usage"]["total"], 30)
        self.assertEqual(validate_snapshot(expired)["sessions"][0]["session_id"], self.sid)
        self.assertEqual(self.collector.db.execute("SELECT count(*) FROM usage").fetchone()[0], 1)
        self.assertEqual(self.path.read_bytes(), source)
        self.assertIn(b'cwo_codex_collector_sessions{} 1', render_session_metrics(expired))
        resumed = self.usage("two")
        resumed["timestamp"] = datetime.fromtimestamp(self.now + 90, timezone.utc).isoformat()
        self.append(resumed)
        self.collector.scan()
        snapshot = self.collector.snapshot(now=self.now + 91)
        self.assertEqual(snapshot["session_export"]["exported_sessions"], 1)
        self.assertEqual(snapshot["sessions"][0]["usage"]["total"], 60)
        self.assertIn(f'session_id="{self.sid}"'.encode(), render_session_metrics(snapshot))

    def test_cap_is_exposition_only_and_reports_omissions(self):
        self.append(self.usage("one"))
        self.collector.scan()
        other = str(uuid4())
        with self.collector.db:
            self.collector._register(other, dict(timestamp=self.now + 1, cwd="/example"))
        snapshot = self.collector.snapshot(now=self.now + 2)
        payload = render_session_metrics(snapshot)
        self.assertEqual(len(snapshot["sessions"]), 2)
        self.assertEqual(len(validate_snapshot(snapshot)["sessions"]), 2)
        self.assertNotIn(f'session_id="{self.sid}"'.encode(), payload)
        self.assertIn(f'session_id="{other}"'.encode(), payload)
        self.assertIn(b'cwo_codex_collector_sessions{} 2', payload)
        self.assertIn(b'cwo_codex_collector_session_export_cap_omitted_sessions{} 1', payload)
        self.assertIn(b'cwo_codex_collector_session_export_cap_truncated{} 1', payload)

    def test_skip_reasons_persist_do_not_repeat_on_scan_and_are_private(self):
        self.append(
            self.event("response_item", {"content": "PRIVATE_OUTPUT"}),
            self.event("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage"}}),
            self.event("turn_context", None),
            self.event("event_msg", {"type": "task_started", "turn_id": "secret"}, at=self.now - 1000),
            self.event("token_usage_record", {"thread_id": str(uuid4())}),
        )
        with self.path.open("a") as out:
            out.write('{"type":"turn_context", INVALID_PRIVATE_RECORD\n')
        self.collector.scan()
        expected = {reason: 0 for reason in SKIP_REASONS}
        expected.update(untracked_prefix=1, untracked_item=1, invalid_payload=1,
                        pre_session_history=1, foreign_session=1, invalid_record=1)
        self.assertEqual(self.counters(), expected)
        self.collector.scan()
        self.assertEqual(self.counters(), expected)
        self.collector.close()
        self.collector = SessionCollector(self.home, self.state)
        self.collector.scan()
        self.assertEqual(self.counters(), expected)
        payload = render_session_metrics(self.collector.snapshot())
        self.assertIn(b'# TYPE cwo_codex_collector_skipped_records_total counter', payload)
        self.assertNotIn(b'PRIVATE', payload)
        self.assertNotIn(b'secret', payload)
        self.assertNotIn(str(self.home).encode(), payload)
        self.assertEqual(set(self.counters()), set(SKIP_REASONS))

    def test_backfill_does_not_double_count_main_reader_rejections(self):
        for item_type in ("CommandExecution", "ContextCompaction"):
            self.append(self.event("event_msg", dict(type="item_completed", thread_id=str(uuid4()),
                turn_id="turn", item=dict(type=item_type, id="item"))))
        self.collector.scan()
        self.assertEqual(self.counters()["foreign_session"], 2)
        with self.collector.db:
            for table in ("command_files", "compaction_files"):
                self.collector.db.execute(f"UPDATE {table} SET offset=0,complete=0")
        self.collector.scan()
        self.assertEqual(self.counters()["foreign_session"], 2)

    def test_skip_counter_and_main_cursor_rollback_together(self):
        self.collector.scan()
        original = self.collector._consume
        self.append(self.event("turn_context", None))
        before = self.collector.db.execute("SELECT offset FROM files").fetchone()[0]
        def fail_after_skip(record, sid, **kwargs):
            original(record, sid, **kwargs)
            raise RuntimeError("synthetic scan crash")
        with patch.object(self.collector, "_consume", fail_after_skip):
            with self.assertRaisesRegex(RuntimeError, "synthetic scan crash"):
                self.collector.scan()
        self.assertEqual(self.counters()["invalid_payload"], 0)
        self.assertEqual(self.collector.db.execute("SELECT offset FROM files").fetchone()[0], before)
        self.collector.scan()
        self.assertEqual(self.counters()["invalid_payload"], 1)

    def test_decoded_rejections_have_fixed_reasons(self):
        self.collector.scan()
        bad_time = self.event("turn_context", {})
        bad_time["timestamp"] = "invalid"
        examples = [
            ([], "non_object_record"),
            (self.event("new_format", {}), "unsupported_record_type"),
            (self.event("event_msg", {"type": "unknown"}), "untracked_event"),
            (bad_time, "invalid_timestamp"),
            (self.event("token_usage_record", {}), "invalid_identity"),
            (self.event("token_usage_record", {"thread_id": self.sid}), "invalid_usage"),
        ]
        with self.collector.db:
            for record, reason in examples:
                self.collector._consume(record, self.sid)
                self.assertEqual(self.counters()[reason], 1, reason)
            self.collector._consume(self.event("turn_context", {}), str(uuid4()))
            self.assertEqual(self.counters()["missing_session"], 1)
        self.assertEqual(sum(self.counters().values()), len(examples) + 1)

    def test_new_counter_migration_does_not_rewind_existing_cursors(self):
        self.append(self.event("response_item", {"content": "not collected"}))
        self.collector.scan()
        offsets = [tuple(row) for row in self.collector.db.execute("SELECT path,offset FROM files")]
        with self.collector.db:
            self.collector.db.execute("DROP TABLE skipped_records")
        self.collector.close()
        self.collector = SessionCollector(self.home, self.state)
        self.collector.scan()
        self.assertEqual([tuple(row) for row in self.collector.db.execute("SELECT path,offset FROM files")], offsets)
        self.assertEqual(sum(self.counters().values()), 0)

    def test_oversized_and_partial_lines_are_counted_once_when_skipped(self):
        self.collector.scan()
        with self.path.open("a") as out:
            out.write('X' * 2500 + '\n')
            out.write('{"type":"turn_context",')
        with patch('traceonaut.codex_session_telemetry.MAX_LINE', 512):
            self.collector.scan()
            self.collector.scan()
        self.assertEqual(self.counters()["oversized_record"], 1)
        self.assertEqual(self.counters()["invalid_record"], 0)
        with self.path.open("a") as out:
            out.write('"payload":null}\n')
        self.collector.scan()
        self.assertEqual(self.counters()["invalid_payload"], 1)

    def test_invalid_header_is_visible_not_a_crash(self):
        (self.home / "sessions" / "rollout-bad.jsonl").write_text('[]\n')
        self.collector.scan()
        self.assertEqual(self.counters()["invalid_metadata"], 1)
        self.assertGreater(self.collector.snapshot()["errors"]["missing_metadata"], 0)

    def test_fresh_collector_exports_known_zero_errors_and_skips(self):
        self.collector.scan()
        payload = render_session_metrics(self.collector.snapshot())
        self.assertIn(b'cwo_codex_collector_errors_total{reason="invalid_record"} 0', payload)
        for reason in SKIP_REASONS:
            self.assertIn(f'cwo_codex_collector_skipped_records_total{{reason="{reason}"}} 0'.encode(), payload)

    def test_invalid_policy_is_rejected_and_defaults_are_explicit(self):
        self.assertEqual(SESSION_RETENTION_SECONDS, 30 * 86400)
        self.assertEqual(DEFAULT_SESSION_EXPORT_CAP, 1000)
        for args in (dict(session_export_cap=0), dict(session_export_cap=100001),
                     dict(session_export_cap=True), dict(session_retention_seconds=-1),
                     dict(session_retention_seconds=1.5), dict(session_retention_seconds=True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                SessionCollector(self.home, Path(self.tmp.name) / "invalid", **args)


if __name__ == '__main__':
    unittest.main()
