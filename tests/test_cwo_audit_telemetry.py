"""CWO workflow source projection, replay, limits and CLI compatibility."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut import cwo_audit_telemetry as audit
from collect_codex_sessions import SessionMetricsEndpoint

NOW = 1800000000


def record(kind="packet_built", timestamp=NOW - 10, **fields):
    event = {"event_type": kind, "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), **fields}
    event["event_hash"] = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    return (json.dumps(event) + "\n").encode()


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "audit.jsonl"
        self.path.write_bytes(record())
        self.collector = audit.AuditCollector(directories=[self.root])

    def test_projection_dedup_and_no_private_fields(self):
        first = record(private="secret fixture prompt /private/example", model="private-model")
        second = record("dispatch_prepared")
        self.path.write_bytes(first + second + first)
        (self.root / "copy-audit.jsonl").write_bytes(first)
        collector = audit.AuditCollector(files=[self.path], directories=[self.root])
        for instance in (collector, collector, audit.AuditCollector(directories=[self.root])):
            snapshot = instance.scan(now=NOW)
            self.assertEqual(snapshot["collection_complete"], 1)
            self.assertEqual(snapshot["source_files"], 2)
            self.assertEqual(len(snapshot["events"]), 2)
            payload = audit.render_audit_metrics(snapshot).decode()
            self.assertNotIn("secret", payload)
            self.assertNotIn("private", payload)
            self.assertNotIn(str(self.root), payload)
            for name in audit.METRICS:
                self.assertIn(f"# HELP {name} ", payload)
                self.assertIn(f"# TYPE {name} gauge", payload)
        self.assertEqual(self.path.read_bytes(), first + second + first)

    def test_cache_hashes_content_and_detects_same_size_same_mtime_rewrite(self):
        before = self.path.stat()
        with mock.patch.object(audit, "_parse", wraps=audit._parse) as parser:
            self.collector.scan(now=NOW)
            self.collector.scan(now=NOW)
            self.assertEqual(parser.call_count, 1)
            self.path.write_bytes(record(timestamp=NOW - 11))
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            snapshot = self.collector.scan(now=NOW)
            self.assertEqual(parser.call_count, 2)
            self.assertEqual(snapshot["events"][0][2], NOW - 11)

    def test_append_rotation_truncation_and_removal(self):
        first = self.collector.scan(now=NOW)
        with self.path.open("ab") as stream:
            stream.write(record("return_evaluated"))
        self.assertEqual(len(self.collector.scan(now=NOW)["events"]), 2)
        self.path.rename(self.root / "rotated-audit.jsonl")
        self.path.write_bytes(record("native_pool_status"))
        self.assertEqual(len(self.collector.scan(now=NOW)["events"]), 3)
        self.path.write_bytes(b"")
        self.assertEqual(len(self.collector.scan(now=NOW)["events"]), 2)
        (self.root / "rotated-audit.jsonl").unlink()
        empty = self.collector.scan(now=NOW)
        self.assertEqual((empty["source_available"], empty["collection_complete"], empty["events"]), (1, 1, []))
        self.path.unlink()
        missing = self.collector.scan(now=NOW)
        self.assertEqual((missing["source_available"], missing["collection_complete"], missing["events"]), (0, 0, []))
        self.assertEqual(len(first["events"]), 1)

    def test_invalid_records_partial_retry_unknown_type_and_bounded_labels(self):
        good = record("unexpected-private-event-name", private="must stay private")
        invalid = json.loads(record()); invalid["event_hash"] = "x" * 64
        self.path.write_bytes(b'garbage\n[]\n{"x":1,"x":2}\n' + json.dumps(invalid).encode() + b"\n" + good[:-1])
        first = self.collector.scan(now=NOW)
        self.assertEqual(first["events"], [])
        self.assertEqual(first["skipped_records"], {"invalid_json": 2, "unsupported_record": 1, "invalid_hash": 1, "partial_line": 1})
        self.assertEqual(first["collection_complete"], 0)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        snapshot = self.collector.scan(now=NOW)
        self.assertEqual(snapshot["events"][0][1], "other")
        self.assertNotIn("unexpected-private", audit.render_audit_metrics(snapshot).decode())
        self.assertNotIn("partial_line", snapshot["skipped_records"])

    def test_retention_cap_future_timestamps_and_timezone(self):
        self.path.write_bytes(record(timestamp=NOW - audit.RETENTION_SECONDS - 1) +
                              record(timestamp=NOW - audit.RETENTION_SECONDS) +
                              record(timestamp=NOW + 1) + record())
        snapshot = self.collector.scan(now=NOW)
        self.assertEqual(len(snapshot["events"]), 2)
        self.assertEqual(snapshot["skipped_records"]["future_timestamp"], 1)
        with mock.patch.object(audit, "EXPORT_CAP", 1):
            snapshot = self.collector.scan(now=NOW)
            self.assertEqual((len(snapshot["events"]), snapshot["limit_reached"], snapshot["collection_complete"]), (1, 1, 0))
            self.assertEqual(snapshot["events"][0][2], NOW - 10)
        self.assertEqual(len(self.collector.scan(now=NOW + 2)["events"]), 2)
        for value in ("2026-09-24T00:00:00", None, 10, "bad"):
            data = {"event_type": "packet_built", "timestamp": value}
            data["event_hash"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.path.write_text(json.dumps(data) + "\n")
            self.assertEqual(self.collector.scan(now=NOW)["skipped_records"], {"invalid_timestamp": 1})

    def test_discovery_names_missing_sources_and_symlinks(self):
        child = self.root / "nested"; child.mkdir()
        (child / "contract-audit.jsonl").write_bytes(record("return_evaluated"))
        (child / "output.jsonl").write_bytes(record("dispatch_prepared"))
        self.assertEqual(len(self.collector.scan(now=NOW)["events"]), 2)
        (child / "linked-audit.jsonl").symlink_to(self.path)
        (child / "loop").symlink_to(self.root, target_is_directory=True)
        snapshot = self.collector.scan(now=NOW)
        self.assertEqual(snapshot["source_errors"], 1)
        self.assertEqual(snapshot["collection_complete"], 0)
        bad = audit.AuditCollector(files=[self.root / "missing", child / "linked-audit.jsonl"])
        snapshot = bad.scan(now=NOW)
        self.assertEqual((snapshot["source_available"], snapshot["source_errors"]), (0, 2))
        link = self.root / "dir-link"; link.symlink_to(child, target_is_directory=True)
        self.assertEqual(audit.AuditCollector(directories=[link]).scan(now=NOW)["source_errors"], 1)

    def test_special_files_limits_and_fresh_discovery(self):
        fifo = self.root / "fifo-audit.jsonl"; os.mkfifo(fifo)
        snapshot = self.collector.scan(now=NOW)
        self.assertEqual(snapshot["source_errors"], 1)
        fifo.unlink()
        for key, value in (("MAX_SCAN_BYTES", 1), ("MAX_FILES", 0), ("MAX_ENTRIES", 0)):
            with mock.patch.object(audit, key, value):
                self.assertEqual(self.collector.scan(now=NOW)["limit_reached"], 1)
        with mock.patch.object(audit, "MAX_LINE_BYTES", 10):
            self.assertEqual(audit.AuditCollector(files=[self.path]).scan(now=NOW)["skipped_records"], {"oversized_line": 1})
        directory = self.root / "new"; directory.mkdir()
        with mock.patch.object(audit, "MAX_DEPTH", 0):
            self.assertEqual(self.collector.scan(now=NOW)["limit_reached"], 1)
        (directory / "audit.jsonl").write_bytes(record("return_evaluated"))
        self.assertEqual(len(self.collector.scan(now=NOW)["events"]), 2)

    def test_cli_optional_input_and_same_endpoint(self):
        home = self.root / "codex"; (home / "sessions").mkdir(parents=True)
        args = [sys.executable, str(ROOT / "scripts/collect_codex_sessions.py"), "--codex-home", str(home),
                "--session-state-dir", str(self.root / "state"), "--snapshot-file", str(self.root / "snapshot.json"), "--once"]
        baseline = subprocess.run(args, capture_output=True, text=True, check=True)
        self.assertNotIn("cwo_audit", json.loads(baseline.stdout))
        result = subprocess.run(args + ["--cwo-audit-file", str(self.path)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["cwo_audit"]["source_available"], 1)
        invalid = subprocess.run(args + ["--cwo-audit-file", "relative.jsonl"], capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        overlap = subprocess.run(args + ["--cwo-audit-file", str(self.root / "snapshot.json")], capture_output=True, text=True)
        self.assertEqual(overlap.returncode, 2)
        overlap = subprocess.run(args + ["--cwo-audit-dir", str(self.root)], capture_output=True, text=True)
        self.assertEqual(overlap.returncode, 2)
        endpoint = SessionMetricsEndpoint("127.0.0.1", 0, b"test-fixture-not-a-real-credential")
        self.addCleanup(endpoint.close)
        snapshot = json.loads((self.root / "snapshot.json").read_text())
        endpoint.update_sessions(snapshot, audit=self.collector.scan(now=NOW))
        self.assertIn(b"cwo_codex_collector_scan_timestamp_seconds", endpoint._payload)
        self.assertIn(b"cwo_audit_event_timestamp_seconds", endpoint._payload)

    def test_invalid_input_count_and_relative_paths(self):
        for kwargs in ({}, {"files": ["relative"]}, {"files": [self.path] * 257}):
            with self.assertRaises(ValueError):
                audit.AuditCollector(**kwargs)


if __name__ == "__main__":
    unittest.main()
