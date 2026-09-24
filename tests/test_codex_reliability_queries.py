"""Execute beta event-time queries against optional isolated Prometheus fixtures."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.observability_exporter import MetricsEndpoint, PrometheusQueryClient


@unittest.skipUnless(os.environ.get("CWO_TEST_PROMETHEUS_BINARY"), "separately verified Prometheus binary not supplied")
class CommandQueryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.now = int(time.time())
        cls.endpoint = MetricsEndpoint("127.0.0.1", 0, b"synthetic-command-fixture")
        # Explicit independent exposition fixture; production rendering is tested separately.
        lines = [
            f"cwo_codex_collector_source_available 1",
            f"cwo_codex_collector_scan_timestamp_seconds {cls.now}",
            f"cwo_codex_command_telemetry_ready 1",
            f"cwo_codex_command_telemetry_snapshot_timestamp_seconds {cls.now}",
            f"cwo_codex_command_telemetry_complete_after_timestamp_seconds {cls.now - 604800}",
            f"cwo_codex_compaction_telemetry_ready 1",
            f"cwo_codex_compaction_telemetry_snapshot_timestamp_seconds {cls.now}",
            f"cwo_codex_compaction_telemetry_complete_after_timestamp_seconds {cls.now - 604800}",
        ]
        for project, session in (
            ("fixture", "work-a"),
            ("fixture", "work-b"),
            ("fixture", "work-zero"),
            ("fixture", "work-missing"),
            ("elsewhere", "work-c"),
        ):
            lines.append(f'cwo_codex_session_info{{project_id="{project}",session_id="{session}",kind="session",parent_id="none",model="unknown",effort="unknown"}} 1')
        for project, session, age in (
            ("fixture", "work-a", 10),
            ("fixture", "work-b", 20),
            ("fixture", "work-zero", 30),
            ("fixture", "work-missing", 40),
            ("elsewhere", "work-c", 50),
        ):
            labels = f'project_id="{project}",session_id="{session}"'
            lines.append(f"cwo_codex_session_last_event_timestamp_seconds{{{labels}}} {cls.now - age}")
            lines.append(f"cwo_codex_session_state{{{labels}}} 2")
        for session, usage_state, tokens in (
            (
                "work-a",
                1,
                {"input": 100, "cached_input": 0, "output": 40, "reasoning_output": 0, "total": 140},
            ),
            (
                "work-b",
                4,
                {"input": 300, "cached_input": 150, "output": 60, "reasoning_output": 30, "total": 360},
            ),
            ("work-zero", 0, {}),
            (
                "work-missing",
                1,
                {"input": 0, "cached_input": 0, "output": 0, "reasoning_output": 0, "total": 0},
            ),
        ):
            labels = f'project_id="fixture",session_id="{session}"'
            lines.append(f"cwo_codex_session_usage_state{{{labels}}} {usage_state}")
            for token_kind, value in tokens.items():
                lines.append(
                    f'cwo_codex_session_usage_tokens{{{labels},token_kind="{token_kind}"}} {value}'
                )
        # A metric without an eligible usage source must remain excluded.
        lines.append(
            'cwo_codex_session_usage_tokens{project_id="fixture",session_id="work-zero",token_kind="total"} 999'
        )
        for session, seconds in (("work-a", 55), ("work-zero", 0)):
            lines.append(
                'cwo_codex_session_observed_turn_seconds'
                f'{{project_id="fixture",session_id="{session}"}} {seconds}'
            )
        # Old failure and another project's record must not inflate selected-range totals.
        for project, session, event, age, outcome, duration in (
            ("fixture", "work-a", "old", 4000, "failed", 999),
            ("fixture", "work-a", "failure", 900, "failed", 40),
            ("fixture", "work-a", "success", 30, "completed", 2),
            ("fixture", "work-b", "unknown", 60, "unknown", 0),
            ("fixture", "work-b", "ambiguous", 90, "unknown", None),
            ("fixture", "work-missing", "missing-duration", 120, "completed", None),
            ("elsewhere", "work-c", "other", 600, "failed", 1000),
        ):
            labels = f'project_id="{project}",session_id="{session}",observation_id="{event}",outcome="{outcome}"'
            lines.append(f"cwo_codex_command_event_timestamp_seconds{{{labels}}} {cls.now - age}")
            if duration is not None:
                lines.append(f"cwo_codex_command_event_duration_seconds{{{labels}}} {duration}")
        for project, session, event, age in (
            ("fixture", "work-a", "old-context", 4000),
            ("fixture", "work-a", "context-one", 900),
            ("fixture", "work-a", "context-two", 60),
            ("fixture", "work-b", "context-three", 30),
            ("elsewhere", "work-c", "other-context", 600),
        ):
            lines.append(f'cwo_codex_compaction_observation_timestamp_seconds{{project_id="{project}",session_id="{session}",observation_id="{event}"}} {cls.now - age}')
        cls.endpoint._payload = ("\n".join(lines) + "\n").encode()
        cls.endpoint.start()
        cls.addClassCleanup(cls.endpoint.close)
        credential = root / "fixture.token"
        credential.write_bytes(b"synthetic-command-fixture")
        credential.chmod(0o600)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        config = root / "prometheus.json"
        config.write_text(json.dumps({"global": {"scrape_interval": "1s"}, "scrape_configs": [{"job_name": "fixture", "scrape_timeout": "1s", "authorization": {"type": "Bearer", "credentials_file": str(credential)}, "static_configs": [{"targets": [f"127.0.0.1:{cls.endpoint.server.server_address[1]}"]}]}]}))
        process = subprocess.Popen([os.environ["CWO_TEST_PROMETHEUS_BINARY"], "--config.file=" + str(config), "--storage.tsdb.path=" + str(root / "tsdb"), f"--web.listen-address=127.0.0.1:{port}", "--storage.tsdb.retention.time=1h", "--log.level=error"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        cls.addClassCleanup(stop)
        cls.client = PrometheusQueryClient(f"http://127.0.0.1:{port}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if cls.client.query("cwo_codex_command_telemetry_ready", time.time()):
                    break
            except (OSError, ValueError):
                pass
            if process.poll() is not None:
                raise AssertionError("temporary Prometheus exited")
            time.sleep(0.1)
        else:
            raise AssertionError("temporary Prometheus did not scrape fixture")
        cls.query_at = time.time()
        dashboard = json.loads((ROOT / "examples/observability/codex-work-overview-beta.json").read_text())
        panels = dashboard["panels"] + [child for p in dashboard["panels"] for child in p.get("panels", [])]
        cls.panels = {p["title"]: p for p in panels if p.get("targets")}
        cls.panel_ids = {p["id"]: p for p in panels if p.get("targets")}

    def query(self, title, *, ref="A", start=None, end=None, project="fixture", session=".+", when=None):
        start = self.now - 1800 if start is None else start
        end = self.now if end is None else end
        panel = self.panel_ids[title] if isinstance(title, int) else self.panels[title]
        expression = next(t["expr"] for t in panel["targets"] if t["refId"] == ref)
        expression = expression.replace("$project", project).replace("$session", session).replace("$__from", str(start * 1000)).replace("$__to", str(end * 1000))
        return self.client.query(expression, self.query_at if when is None else when)

    def value(self, title, **kwargs):
        rows = self.query(title, **kwargs)
        self.assertEqual(len(rows), 1)
        return float(rows[0]["value"][1])

    def values_by_session(self, title, **kwargs):
        return {
            row["metric"]["session_id"]: float(row["value"][1])
            for row in self.query(title, **kwargs)
        }

    def counts(self, identity, **kwargs):
        """The two mutually exclusive headline frames are presentation, not sums."""
        covered = self.query(identity, ref="A", **kwargs)
        partial = self.query(identity, ref="B", **kwargs)
        self.assertFalse(covered and partial)
        return covered or partial

    def count(self, identity, **kwargs):
        rows = self.counts(identity, **kwargs)
        self.assertEqual(len(rows), 1)
        return float(rows[0]["value"][1])

    def publish(self, raw, marker, value):
        if marker == "fixture_generation":
            raw = b"\n".join(line for line in raw.split(b"\n") if not line.startswith(b"fixture_generation "))
            raw += f"\nfixture_generation {value}\n".encode()
        with self.endpoint._lock:
            self.endpoint._payload = raw
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = self.client.query(marker, time.time())
            if rows and float(rows[0]["value"][1]) == value:
                return time.time()
            time.sleep(.05)
        self.fail("Prometheus did not observe the changed fixture")

    def test_counts_filter_completion_time_both_bounds_and_project(self):
        self.assertEqual(self.count(51), 5)
        self.assertEqual(self.count(52), 1)
        self.assertEqual(self.count(51, end=self.now - 45), 4)
        self.assertEqual(self.count(51, start=self.now - 45), 1)
        self.assertEqual(self.count(52, project="elsewhere"), 1)
        self.assertEqual(self.values_by_session(11, ref="E")["work-a"], 40)
        # Both source-time boundaries are inclusive.
        self.assertEqual(self.count(51, start=self.now - 30, end=self.now - 30), 1)

    def test_complete_empty_range_is_zero_but_missing_duration_stays_missing(self):
        self.assertEqual(self.count(51, session="absent"), 0)
        self.assertEqual(self.count(52, session="absent"), 0)
        for work, count in (("work-zero", 0), ("work-missing", 1)):
            self.assertEqual(self.values_by_session(11, ref="F", session=work), {work: count})
            self.assertTrue(math.isnan(self.values_by_session(11, ref="E", session=work)[work]))
        self.assertEqual(self.values_by_session(11, ref="E", session="work-b"), {"work-b": 0})

    def test_watermark_changes_coverage_not_observed_values(self):
        for start in (self.now - 604800, self.now - 604801):
            self.assertEqual(self.query(51, start=start), [])
            self.assertEqual(self.value(51, ref="B", start=start), 6)
            self.assertEqual(self.value(52, ref="B", start=start), 2)
            self.assertEqual(self.value(58, start=start), 0)
            self.assertEqual(self.values_by_session(11, ref="F", start=start)["work-a"], 3)
        for when in (self.now + 30, self.now - 1):
            for identity in (51, 52):
                self.assertEqual(self.counts(identity, when=when), [])
            for ref in ("D", "E", "F", "I"):
                self.assertEqual(self.query(11, ref=ref, when=when), [])
            self.assertEqual(self.query(58, when=when), [])

    def test_partial_gaps_backfill_and_cap_keep_positive_and_zero_lower_bounds(self):
        original = self.endpoint._payload
        partial = original.replace(b"cwo_codex_command_telemetry_ready 1\n", b"cwo_codex_command_telemetry_ready 0\n")
        partial += (b"cwo_codex_command_telemetry_source_gaps 1\n"
                    b"cwo_codex_command_telemetry_cap_truncated 1\n"
                    b"cwo_codex_command_telemetry_pending_files 5\n")
        try:
            when = self.publish(partial, "cwo_codex_command_telemetry_ready", 0)
            self.assertEqual(self.query(51, when=when), [])
            self.assertEqual(self.value(51, ref="B", when=when), 5)
            self.assertEqual(self.value(52, ref="B", when=when), 1)
            self.assertEqual(self.value(51, ref="B", session="absent", when=when), 0)
            self.assertEqual(self.value(58, when=when), 0)
            self.assertEqual(self.values_by_session(11, ref="F", when=when),
                             {"work-a": 2, "work-b": 2, "work-zero": 0, "work-missing": 1})
            self.assertEqual(self.values_by_session(11, ref="I", when=when)["work-b"], 2)
            self.assertEqual(self.values_by_session(11, ref="J", when=when)["work-a"], 2)
        finally:
            self.publish(original, "cwo_codex_command_telemetry_ready", 1)

    def test_missing_source_never_becomes_a_partial_zero(self):
        original = self.endpoint._payload
        try:
            when = self.publish(original.replace(b"cwo_codex_collector_source_available 1\n", b"cwo_codex_collector_source_available 0\n"), "cwo_codex_collector_source_available", 0)
            for identity in (51, 52):
                self.assertEqual(self.counts(identity, when=when), [])
            self.assertEqual(self.query(58, when=when), [])
            self.assertEqual(self.query(11, ref="F", when=when), [])
        finally:
            self.publish(original, "cwo_codex_collector_source_available", 1)

    def test_duplicate_event_identity_is_counted_once(self):
        original = self.endpoint._payload
        duplicate = original + (f'cwo_codex_command_event_timestamp_seconds{{project_id="fixture",session_id="work-a",observation_id="failure",outcome="failed",replica="second"}} {self.now - 900}\nfixture_generation 2\n').encode()
        try:
            when = self.publish(duplicate, "fixture_generation", 2)
            self.assertEqual(self.count(51, when=when), 5)
            self.assertEqual(self.count(52, when=when), 1)
        finally:
            self.publish(original + b"fixture_generation 1\n", "fixture_generation", 1)

    def test_consolidated_work_table_keeps_numeric_sortable_values(self):
        rows = self.query(11, ref="A")
        self.assertEqual({r["metric"]["session_id"] for r in rows}, {"work-a", "work-b", "work-zero", "work-missing"})
        self.assertTrue(all(r["metric"]["work"] == r["metric"]["session_id"] for r in rows))
        self.assertTrue(all(r["metric"]["kind"] == "session" for r in rows))
        failures = self.values_by_session(11, ref="D")
        commands = self.values_by_session(11, ref="F")
        self.assertEqual(failures, {"work-a": 1, "work-b": 0, "work-zero": 0, "work-missing": 0})
        self.assertEqual(sum(failures.values()), self.count(52))
        self.assertEqual(sum(commands.values()), self.count(51))

    def test_last_seen_is_anchored_to_range_end(self):
        end = self.now - 5
        expected = {"work-a": 5, "work-b": 15, "work-zero": 25, "work-missing": 35}
        for when in (self.query_at, self.query_at + 10):
            self.assertEqual(self.values_by_session(11, ref="C", start=self.now - 60, end=end, when=when), expected)
        self.assertEqual(self.values_by_session(11, ref="G", end=end)["work-a"], (self.now - 10) * 1000)

    def test_ranked_history_preserves_eligibility_zero_missing_and_range_scope(self):
        self.assertEqual(self.values_by_session(80), {"work-a": 140, "work-b": 360, "work-missing": 0})
        self.assertEqual(self.values_by_session(81), {"work-a": 55, "work-zero": 0})
        for identity, expected in ((80, 140), (81, 55)):
            self.assertEqual(self.value(identity, session="work-a", start=self.now - 15, end=self.now - 5), expected)
        self.assertEqual(self.query(80, session="work-zero"), [])
        self.assertEqual(self.query(81, session="work-b"), [])

    def test_history_coverage_and_share_ratios_remain_distinct(self):
        for session, expected in (("work-a", 1), ("work-missing", 1), ("work-b", 2), ("work-zero", 2), ("absent", 0)):
            self.assertEqual(self.value(39, session=session), expected)
        self.assertAlmostEqual(self.value(34), 37.5)
        self.assertAlmostEqual(self.value(36), 30)
        self.assertEqual(self.value(34, session="work-a"), 0)
        for session in ("work-zero", "work-missing", "absent"):
            self.assertEqual(self.query(34, session=session), [])
            self.assertEqual(self.query(36, session=session), [])

    def test_compactions_keep_independent_coverage_and_source_time(self):
        self.assertEqual(self.values_by_session(11, ref="J"), {"work-a": 2, "work-b": 1, "work-zero": 0, "work-missing": 0})
        self.assertEqual(self.values_by_session(11, ref="J", start=self.now - 60)["work-a"], 1)
        self.assertEqual(self.values_by_session(11, ref="J", end=self.now - 60), {})
        original = self.endpoint._payload
        try:
            when = self.publish(original.replace(b"cwo_codex_compaction_telemetry_ready 1\n", b"cwo_codex_compaction_telemetry_ready 0\n"), "cwo_codex_compaction_telemetry_ready", 0)
            self.assertEqual(self.values_by_session(11, ref="J", when=when), {"work-a": 2, "work-b": 1})
            self.assertEqual(self.value(76, ref="B", when=when), 0)
            self.assertEqual(self.count(51, when=when), 5)
        finally:
            self.publish(original, "cwo_codex_compaction_telemetry_ready", 1)

    def test_unknown_history_start_is_partial_instead_of_missing(self):
        original = self.endpoint._payload
        missing = b"\n".join(line for line in original.split(b"\n") if b"complete_after_timestamp_seconds" not in line)
        try:
            when = self.publish(missing + b"fixture_generation 3\n", "fixture_generation", 3)
            self.assertEqual(self.query(51, when=when), [])
            self.assertEqual(self.value(51, ref="B", when=when), 5)
            self.assertEqual(self.value(58, when=when), 0)
            self.assertEqual(self.value(76, ref="A", when=when), 0)
            self.assertEqual(self.value(76, ref="B", when=when), 0)
            self.assertEqual(self.values_by_session(11, ref="F", when=when)["work-a"], 2)
        finally:
            self.publish(original + b"fixture_generation 4\n", "fixture_generation", 4)
