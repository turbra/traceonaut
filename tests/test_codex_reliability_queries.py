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
        # Independent state fixtures include complete, incomplete and conflicting
        # populations. The original command/history fixtures stay unchanged.
        for project, sessions in (
            ("ring", [("unknown", 0), ("working", 1), ("waiting", 2), ("stopped", 3), ("quiet", 4)]),
            ("ring-missing", [("known", 2), ("absent-state", None)]),
            ("ring-invalid", [("known", 2), ("unsupported", 9)]),
            ("ring-fractional", [("known", 2), ("unsupported", 2.5)]),
            ("ring-conflict", [("known", 2), ("conflicting", 1)]),
        ):
            for session, state in sessions:
                labels = f'project_id="{project}",session_id="{session}"'
                lines.append(f'cwo_codex_session_info{{{labels}}} 1')
                lines.append(f'cwo_codex_session_last_event_timestamp_seconds{{{labels}}} {cls.now - 10}')
                if state is not None:
                    lines.append(f'cwo_codex_session_state{{{labels}}} {state}')
        lines.extend([
            'cwo_codex_session_state{project_id="ring",session_id="working",replica="second"} 1',
            'cwo_codex_session_info{project_id="ring",session_id="working",replica="second"} 1',
            'cwo_codex_session_state{project_id="ring-conflict",session_id="conflicting",replica="second"} 2',
            'cwo_codex_session_state{project_id="ring",session_id="orphan"} 1',
            f'cwo_codex_session_last_event_timestamp_seconds{{project_id="ring",session_id="orphan"}} {cls.now - 10}',
            'cwo_codex_session_info{project_id="ring",session_id="old"} 1',
            'cwo_codex_session_state{project_id="ring",session_id="old"} 2',
            f'cwo_codex_session_last_event_timestamp_seconds{{project_id="ring",session_id="old"}} {cls.now - 4000}',
        ])
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
        dashboard = json.loads((ROOT / "examples/observability/grafana-codex-beta-dashboard.json").read_text())
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

    def assert_work_nan(self, ref, *, expected=None, **kwargs):
        values = self.values_by_session(11, ref=ref, **kwargs)
        self.assertEqual(
            set(values),
            expected or {"work-a", "work-b", "work-zero", "work-missing"},
        )
        self.assertTrue(all(math.isnan(value) for value in values.values()))

    def test_counts_filter_completion_time_both_bounds_and_project(self):
        self.assertEqual(self.value("Commands recorded"), 5)
        self.assertEqual(self.value("Recorded failures"), 1)
        self.assertEqual(self.value("Unknown outcomes"), 2)
        self.assertEqual(self.value("Commands recorded", end=self.now - 45), 4)
        self.assertEqual(self.value("Commands recorded", start=self.now - 45), 1)
        self.assertEqual(self.value("Recorded failures", project="elsewhere"), 1)
        self.assertEqual(self.value("Longest recorded command"), 40)

    def ring_counts(self, **kwargs):
        return {
            target["legendFormat"]: float(rows[0]["value"][1])
            for target in self.panel_ids[23]["targets"]
            if target["refId"] in "ABCDE" and (rows := self.query(23, ref=target["refId"], **kwargs))
        }

    def test_ring_counts_selected_sessions_once_and_excludes_orphan_and_old_state(self):
        counts = self.ring_counts(project="ring")
        self.assertEqual(counts, {"Working": 1, "Waiting": 1, "Stopped / failed": 1,
                                  "No recent signal": 1, "Unknown": 1})
        self.assertEqual(sum(counts.values()), self.value(3, project="ring"))
        self.assertEqual(self.ring_counts(project="ring", session="working"), {"Working": 1})
        self.assertEqual(self.ring_counts(project="ring", session="waiting"), {"Waiting": 1})
        self.assertEqual(self.ring_counts(project="ring", session="unknown"), {"Unknown": 1})
        self.assertEqual(self.ring_counts(project="ring", session="orphan"), {})

    def test_ring_never_renders_a_partial_distribution_as_a_complete_ring(self):
        for project in ("ring-missing", "ring-invalid", "ring-fractional", "ring-conflict"):
            with self.subTest(project=project):
                self.assertEqual(self.value(3, project=project), 2)
                self.assertEqual(self.ring_counts(project=project), {})
                self.assertEqual(self.ring_counts(project=project, session="known"), {"Waiting": 1})

    def test_ring_empty_stale_and_historical_bounds_do_not_create_slices(self):
        self.assertEqual(self.value(3, project="ring", session="absent"), 0)
        self.assertEqual(self.ring_counts(project="ring", session="absent"), {})
        self.assertEqual(self.ring_counts(project="ring", end=self.now - 11), {})
        self.assertEqual(self.ring_counts(project="ring", start=self.now - 9), {})
        self.assertEqual(self.ring_counts(project="ring", when=self.now + 30), {})
        self.assertEqual(self.ring_counts(project="ring", when=self.now - 1), {})

    def test_ring_status_messages_distinguish_empty_from_unavailable_without_slices(self):
        for selection in ({"project": "ring", "session": "absent"},
                          {"project": "ring", "session": "orphan"},
                          {"project": "ring", "end": self.now - 11},
                          {"project": "ring", "start": self.now - 9}):
            with self.subTest(selection=selection):
                self.assertEqual(self.value(3, **selection), 0)
                self.assertTrue(math.isnan(self.value(23, ref="F", **selection)))
                self.assertEqual(self.query(23, ref="G", **selection), [])
                self.assertEqual(self.ring_counts(**selection), {})
        for selection in ({"project": "ring-missing"}, {"project": "ring-invalid"},
                          {"project": "ring-fractional"}, {"project": "ring-conflict"},
                          {"project": "ring", "when": self.now + 30},
                          {"project": "ring", "when": self.now - 1},
                          {"project": "ring", "session": "absent", "when": self.now + 30}):
            with self.subTest(selection=selection):
                self.assertEqual(self.query(23, ref="F", **selection), [])
                self.assertTrue(math.isnan(self.value(23, ref="G", **selection)))
                self.assertEqual(self.ring_counts(**selection), {})
        for session in (".+", "working", "waiting", "unknown"):
            with self.subTest(session=session):
                for ref in ("F", "G"):
                    self.assertEqual(self.query(23, ref=ref, project="ring", session=session), [])

    def test_empty_covered_range_has_zero_counts_but_no_fake_duration(self):
        self.assertEqual(self.value("Commands recorded", session="absent"), 0)
        self.assertEqual(self.value("Recorded failures", session="absent"), 0)
        self.assertEqual(self.query("Longest recorded command", session="absent"), [])
        self.assertEqual(self.value("Longest recorded command", session="work-b"), 0)
        self.assertEqual(
            self.values_by_session(11, ref="D", session="work-zero"),
            {"work-zero": 0},
        )
        self.assertEqual(
            self.values_by_session(11, ref="F", session="work-zero"),
            {"work-zero": 0},
        )
        self.assert_work_nan("E", expected={"work-zero"}, session="work-zero")

    def test_missing_command_duration_stays_missing_while_count_is_recorded(self):
        self.assertEqual(
            self.values_by_session(11, ref="D", session="work-missing"),
            {"work-missing": 0},
        )
        self.assertEqual(
            self.values_by_session(11, ref="F", session="work-missing"),
            {"work-missing": 1},
        )
        self.assert_work_nan("E", expected={"work-missing"}, session="work-missing")
        self.assertEqual(self.query("Longest recorded command", session="work-missing"), [])

    def test_watermark_is_exclusive_and_pre_activation_or_stale_is_unavailable(self):
        for title in ("Commands recorded", "Recorded failures", "Longest recorded command"):
            self.assertEqual(self.query(title, start=self.now - 604800), [], title)
            self.assertEqual(self.query(title, start=self.now - 604801), [], title)
            self.assertEqual(self.query(title, when=self.now + 30), [], title)
            self.assertEqual(self.query(title, when=self.now - 1), [], title)
        for ref in ("D", "E", "F"):
            self.assert_work_nan(ref, start=self.now - 604800)
            self.assert_work_nan(ref, start=self.now - 604801)
            self.assertEqual(self.query(11, ref=ref, when=self.now + 30), [], ref)
            self.assertEqual(self.query(11, ref=ref, when=self.now - 1), [], ref)
        self.assertEqual(self.value(58, start=self.now - 604800), 0)
        self.assertEqual(self.value("Unknown outcomes", start=self.now - 604800), 2)
        self.assertEqual(self.query("Unknown outcomes", when=self.now + 30), [])
        self.assertEqual(self.query("Unknown outcomes", when=self.now - 1), [])

    def test_consolidated_work_table_preserves_per_work_command_semantics(self):
        rows = self.query(11, ref="A")
        self.assertEqual(
            {row["metric"]["session_id"] for row in rows},
            {"work-a", "work-b", "work-zero", "work-missing"},
        )
        self.assertTrue(
            all(row["metric"]["work"] == row["metric"]["session_id"] for row in rows)
        )
        failures = self.values_by_session(11, ref="D")
        longest = self.values_by_session(11, ref="E")
        commands = self.values_by_session(11, ref="F")
        self.assertEqual(
            failures,
            {"work-a": 1, "work-b": 0, "work-zero": 0, "work-missing": 0},
        )
        self.assertEqual(longest["work-a"], 40)
        self.assertEqual(longest["work-b"], 0)
        self.assertTrue(math.isnan(longest["work-zero"]))
        self.assertTrue(math.isnan(longest["work-missing"]))
        self.assertEqual(
            commands,
            {"work-a": 2, "work-b": 2, "work-zero": 0, "work-missing": 1},
        )
        self.assertEqual(sum(failures.values()), self.value("Recorded failures"))
        self.assertEqual(sum(commands.values()), self.value("Commands recorded"))

    def test_last_observed_age_is_anchored_to_range_end_and_keeps_exact_time(self):
        end = self.now - 5
        expected_ages = {
            "work-a": 5,
            "work-b": 15,
            "work-zero": 25,
            "work-missing": 35,
        }
        self.assertEqual(
            self.values_by_session(11, ref="C", start=self.now - 60, end=end),
            expected_ages,
        )
        self.assertEqual(
            self.values_by_session(
                11,
                ref="C",
                start=self.now - 60,
                end=end,
                when=self.query_at + 10,
            ),
            expected_ages,
        )
        self.assertEqual(
            self.values_by_session(11, ref="G", start=self.now - 60, end=end),
            {
                "work-a": (self.now - 10) * 1000,
                "work-b": (self.now - 20) * 1000,
                "work-zero": (self.now - 30) * 1000,
                "work-missing": (self.now - 40) * 1000,
            },
        )

    def test_ranked_history_keeps_source_eligibility_zero_and_missing_distinct(self):
        self.assertEqual(
            self.values_by_session(80),
            {"work-a": 140, "work-b": 360, "work-missing": 0},
        )
        self.assertEqual(
            self.values_by_session(81),
            {"work-a": 55, "work-zero": 0},
        )
        self.assertEqual(self.query(80, session="work-zero"), [])
        self.assertEqual(self.query(81, session="work-b"), [])

    def test_ranked_history_values_are_not_selected_interval_spend(self):
        default_tokens = self.value(80, session="work-a")
        default_turn_time = self.value(81, session="work-a")
        historical_scope = {
            "start": self.now - 15,
            "end": self.now - 5,
            "session": "work-a",
        }
        self.assertEqual(self.value(80, **historical_scope), default_tokens)
        self.assertEqual(self.value(81, **historical_scope), default_turn_time)
        self.assertEqual(default_tokens, 140)
        self.assertEqual(default_turn_time, 55)

    def test_unknown_outcome_table_keeps_positive_lower_bound(self):
        self.assertEqual(self.value("Work with unknown outcomes", ref="B"), 2)
        self.assertEqual(self.query("Work with unknown outcomes")[0]["metric"]["work"], "work-b")

    def test_history_coverage_distinguishes_complete_partial_missing_and_empty(self):
        self.assertEqual(self.value("History coverage", session="work-a"), 1)
        self.assertEqual(self.value("History coverage", session="work-missing"), 1)
        self.assertEqual(self.value("History coverage", session="work-b"), 2)
        self.assertEqual(self.value("History coverage", session="work-zero"), 2)
        self.assertEqual(self.value("History coverage"), 2)
        self.assertEqual(self.value("History coverage", session="absent"), 0)
        self.assertEqual(self.query("History coverage", when=self.now + 30), [])

    def test_history_share_ratios_keep_denominators_and_missingness_distinct(self):
        self.assertAlmostEqual(self.value("Cached input / total input"), 37.5)
        self.assertAlmostEqual(self.value("Reasoning output / total output"), 30)
        self.assertEqual(self.value("Cached input / total input", session="work-a"), 0)
        self.assertEqual(self.value("Reasoning output / total output", session="work-a"), 0)
        self.assertEqual(self.value("Cached input / total input", session="work-b"), 50)
        self.assertEqual(self.value("Reasoning output / total output", session="work-b"), 50)
        for session in ("work-missing", "work-zero", "absent"):
            self.assertEqual(self.query("Cached input / total input", session=session), [])
            self.assertEqual(self.query("Reasoning output / total output", session=session), [])

    def test_unready_collection_suppresses_zero_and_positive_counts(self):
        original = self.endpoint._payload
        with self.endpoint._lock:
            self.endpoint._payload = original.replace(
                b"cwo_codex_command_telemetry_ready 1\n",
                b"cwo_codex_command_telemetry_ready 0\n",
            ).replace(b'observation_id="ambiguous",outcome="unknown"', b'observation_id="ambiguous",outcome="conflict"')
        def wait_for(value):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = self.client.query("cwo_codex_command_telemetry_ready", time.time())
                if rows and float(rows[0]["value"][1]) == value:
                    return time.time()
                time.sleep(0.05)
            self.fail("temporary Prometheus did not observe readiness change")
        try:
            when = wait_for(0)
            for title in ("Commands recorded", "Recorded failures", "Longest recorded command"):
                self.assertEqual(self.query(title, when=when), [], title)
                self.assertEqual(self.query(title, when=when, session="absent"), [], title)
            for ref in ("D", "E", "F"):
                self.assert_work_nan(ref, when=when)
            self.assertEqual(self.value(58, when=when), 0)
            self.assertEqual(self.value("Unknown outcomes", when=when), 2)
            self.assertEqual(self.value("Work with unknown outcomes", ref="B", when=when), 2)
            self.assertEqual(self.query("Unknown outcomes", when=when, session="absent"), [])
        finally:
            with self.endpoint._lock:
                self.endpoint._payload = original
            wait_for(1)

    def test_compaction_event_bounds_and_named_work(self):
        self.assertEqual(self.value("Observed compactions"), 3)
        self.assertEqual(self.value("Observed compactions", start=self.now - 60), 2)
        self.assertEqual(self.value("Observed compactions", end=self.now - 60), 2)
        self.assertEqual(self.value("Observed compactions", project="elsewhere"), 1)
        self.assertEqual(self.value("Observed compactions", session="absent"), 0)
        rows = self.query("Work with observed compactions")
        self.assertEqual({r["metric"]["work"] for r in rows}, {"work-a", "work-b"})
        self.assertEqual(self.value("Work with observed compactions", ref="B", session="work-a"), 2)

    def test_compaction_uncovered_stale_and_pre_activation_are_unavailable(self):
        for title in ("Observed compactions", "Work with observed compactions"):
            self.assertEqual(self.query(title, start=self.now - 604800), [])
            self.assertEqual(self.query(title, start=self.now - 604801), [])
            self.assertEqual(self.query(title, when=self.now + 30), [])
            self.assertEqual(self.query(title, when=self.now - 1), [])
        self.assertEqual(self.value("Compaction coverage", start=self.now - 604800), 0)

    def test_compaction_readiness_gates_zero_and_does_not_change_commands(self):
        original = self.endpoint._payload
        with self.endpoint._lock:
            self.endpoint._payload = original.replace(
                b"cwo_codex_compaction_telemetry_ready 1\n",
                b"cwo_codex_compaction_telemetry_ready 0\n",
            )
        def wait_for(value):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = self.client.query("cwo_codex_compaction_telemetry_ready", time.time())
                if rows and float(rows[0]["value"][1]) == value:
                    return time.time()
                time.sleep(0.05)
            self.fail("temporary Prometheus did not observe compaction readiness change")
        try:
            when = wait_for(0)
            self.assertEqual(self.query("Observed compactions", when=when), [])
            self.assertEqual(self.query("Observed compactions", when=when, session="absent"), [])
            self.assertEqual(self.query("Work with observed compactions", when=when), [])
            self.assertEqual(self.value("Compaction coverage", when=when), 0)
            self.assertEqual(self.value("Commands recorded", when=when), 5)
        finally:
            with self.endpoint._lock:
                self.endpoint._payload = original
            wait_for(1)
