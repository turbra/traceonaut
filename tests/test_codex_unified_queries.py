"""Exercise the Unified dashboard's PromQL against isolated session fixtures."""

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


OBSERVABILITY = ROOT / "examples" / "observability"
UNIFIED = OBSERVABILITY / "grafana-codex-unified-dashboard.json"
BETA = OBSERVABILITY / "grafana-codex-beta-dashboard.json"


def panels_by_id(dashboard: dict) -> dict[int, dict]:
    result: dict[int, dict] = {}

    def visit(panels: list[dict]) -> None:
        for panel in panels:
            result[panel["id"]] = panel
            visit(panel.get("panels", []))

    visit(dashboard["panels"])
    return result


class UnifiedInheritedQueryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.unified = panels_by_id(json.loads(UNIFIED.read_text(encoding="utf-8")))
        cls.beta = panels_by_id(json.loads(BETA.read_text(encoding="utf-8")))

    def test_account_queries_are_inherited_unchanged(self) -> None:
        for panel_id in range(91, 95):
            with self.subTest(panel_id=panel_id):
                self.assertEqual(
                    self.unified[panel_id]["targets"], self.beta[panel_id]["targets"]
                )

    def test_command_and_compaction_queries_are_inherited_unchanged(self) -> None:
        for panel_id in (51, 52, 53, 54, 58, 59, 61, 71, 73, 74, 75):
            with self.subTest(panel_id=panel_id):
                unified = {
                    target["refId"]: (
                        target["expr"],
                        target.get("instant"),
                        target.get("range"),
                    )
                    for target in self.unified[panel_id]["targets"]
                }
                inherited = {
                    target["refId"]: (
                        target["expr"],
                        target.get("instant"),
                        target.get("range"),
                    )
                    for target in self.beta[panel_id]["targets"]
                }
                self.assertEqual(
                    {ref: unified[ref] for ref in inherited}, inherited
                )


@unittest.skipUnless(
    os.environ.get("CWO_TEST_PROMETHEUS_BINARY"),
    "separately verified Prometheus binary not supplied",
)
class UnifiedQueryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.now = int(time.time())
        credential = b"synthetic-unified-fixture"
        lines = [
            "cwo_codex_collector_source_available 1",
            f"cwo_codex_collector_scan_timestamp_seconds {cls.now}",
            f"cwo_codex_collector_last_event_timestamp_seconds {cls.now - 5}",
            "cwo_codex_command_telemetry_ready 1",
            f"cwo_codex_command_telemetry_snapshot_timestamp_seconds {cls.now}",
            f"cwo_codex_command_telemetry_complete_after_timestamp_seconds {cls.now - 604800}",
            "cwo_codex_compaction_telemetry_ready 1",
            f"cwo_codex_compaction_telemetry_snapshot_timestamp_seconds {cls.now}",
            f"cwo_codex_compaction_telemetry_complete_after_timestamp_seconds {cls.now - 604800}",
        ]

        def info(
            project: str,
            session: str,
            *,
            kind: str = "session",
            model: str = "model-a",
            effort: str = "medium",
            parent: str = "none",
            extra: str = "",
        ) -> None:
            labels = (
                f'project_id="{project}",session_id="{session}",kind="{kind}",'
                f'parent_id="{parent}",model="{model}",effort="{effort}"{extra}'
            )
            lines.append(f"cwo_codex_session_info{{{labels}}} 1")

        def observed(project: str, session: str, state: float | None = 2) -> None:
            labels = f'project_id="{project}",session_id="{session}"'
            lines.append(
                f"cwo_codex_session_last_event_timestamp_seconds{{{labels}}} {cls.now - 10}"
            )
            if state is not None:
                lines.append(f"cwo_codex_session_state{{{labels}}} {state}")

        def usage(
            project: str,
            session: str,
            state: int,
            *,
            tokens: dict[str, int] | None = None,
            responses: int | None = None,
        ) -> None:
            labels = f'project_id="{project}",session_id="{session}"'
            lines.append(f"cwo_codex_session_usage_state{{{labels}}} {state}")
            for kind, value in (tokens or {}).items():
                lines.append(
                    f'cwo_codex_session_usage_tokens{{{labels},token_kind="{kind}"}} {value}'
                )
            if responses is not None:
                lines.append(f"cwo_codex_session_response_count{{{labels}}} {responses}")

        complete_tokens = {
            "input": 100,
            "cached_input": 20,
            "output": 40,
            "reasoning_output": 10,
            "total": 140,
        }
        partial_tokens = {
            "input": 300,
            "cached_input": 150,
            "output": 60,
            "reasoning_output": 30,
            "total": 360,
        }
        leaked_tokens = {
            "input": 999,
            "cached_input": 999,
            "output": 999,
            "reasoning_output": 999,
            "total": 1998,
        }

        # One selected population with complete, partial, unknown and conflicted
        # usage. Orphan metrics have no session-info identity and must not join.
        for session, kind, state in (
            ("complete", "session", 1),
            ("partial", "subagent", 2),
            ("unknown", "internal", 2),
            ("conflicted", "session", 2),
        ):
            info("mixed", session, kind=kind)
            observed("mixed", session, state)
        usage("mixed", "complete", 1, tokens=complete_tokens, responses=2)
        usage("mixed", "partial", 4, tokens=partial_tokens, responses=3)
        usage("mixed", "unknown", 0, tokens=leaked_tokens, responses=9)
        usage("mixed", "conflicted", 3, tokens=leaked_tokens, responses=7)
        for session, seconds in (("complete", 55), ("partial", 0), ("unknown", 7)):
            lines.append(
                "cwo_codex_session_observed_turn_seconds"
                f'{{project_id="mixed",session_id="{session}"}} {seconds}'
            )
        observed("mixed", "orphan", 1)
        usage("mixed", "orphan", 1, tokens=leaked_tokens, responses=11)
        lines.append(
            'cwo_codex_session_observed_turn_seconds{project_id="mixed",session_id="orphan"} 999'
        )

        # A singleton proves that trustworthy category zeroes remain zero.
        info("single", "only-waiting")
        observed("single", "only-waiting", 2)

        # Missing and contradictory state series invalidate aggregate state counts.
        for project in ("state-missing", "state-conflict"):
            for session in ("known", "uncertain"):
                info(project, session)
                observed(project, session, 2 if session == "known" else None)
        lines.extend(
            [
                'cwo_codex_session_state{project_id="state-conflict",session_id="uncertain",replica="one"} 1',
                'cwo_codex_session_state{project_id="state-conflict",session_id="uncertain",replica="two"} 2',
            ]
        )

        # Activity edge cases prove that filtering eligible keys before the
        # state aggregation is equivalent to filtering the aggregated state.
        for session in (
            "eligible-working",
            "eligible-waiting",
            "duplicated-working",
            "conflicting-state",
            "old-timestamp",
            "future-timestamp",
            "missing-timestamp",
            "duplicated-timestamp",
            "conflicting-timestamp",
        ):
            info("activity-edge", session)
        for session, state in (
            ("eligible-working", 1),
            ("eligible-waiting", 2),
            ("old-timestamp", 1),
            ("future-timestamp", 1),
            ("missing-timestamp", 1),
            ("duplicated-timestamp", 1),
            ("conflicting-timestamp", 1),
        ):
            lines.append(
                "cwo_codex_session_state"
                f'{{project_id="activity-edge",session_id="{session}"}} {state}'
            )
        lines.extend(
            [
                'cwo_codex_session_state{project_id="activity-edge",session_id="duplicated-working",replica="one"} 1',
                'cwo_codex_session_state{project_id="activity-edge",session_id="duplicated-working",replica="two"} 1',
                'cwo_codex_session_state{project_id="activity-edge",session_id="conflicting-state",replica="one"} 1',
                'cwo_codex_session_state{project_id="activity-edge",session_id="conflicting-state",replica="two"} 2',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="eligible-working"}} {cls.now - 10}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="eligible-waiting"}} {cls.now - 10}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="duplicated-working"}} {cls.now - 10}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="conflicting-state"}} {cls.now - 10}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="old-timestamp"}} {cls.now - 7200}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="future-timestamp"}} {cls.now + 60}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="duplicated-timestamp",replica="old"}} {cls.now - 7200}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="duplicated-timestamp",replica="eligible"}} {cls.now - 10}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="conflicting-timestamp",replica="eligible"}} {cls.now - 10}',
                f'cwo_codex_session_last_event_timestamp_seconds{{project_id="activity-edge",session_id="conflicting-timestamp",replica="future"}} {cls.now + 60}',
            ]
        )

        # Recorded-turn fixtures distinguish a trustworthy zero, no recorded
        # metric, and a known lower bound when interval coverage is incomplete.
        for project, sessions in (
            ("turns-recorded", ("one", "two")),
            ("turns-zero", ("one",)),
            ("turns-missing", ("one",)),
            ("turns-partial", ("recorded", "missing")),
        ):
            for session in sessions:
                info(project, session)
                observed(project, session, 2)
        lines.extend(
            [
                'cwo_codex_session_completed_turns{project_id="turns-recorded",session_id="one"} 5',
                'cwo_codex_session_failed_turns{project_id="turns-recorded",session_id="one"} 1',
                'cwo_codex_session_completed_turns{project_id="turns-recorded",session_id="two"} 0',
                'cwo_codex_session_failed_turns{project_id="turns-recorded",session_id="two"} 0',
                'cwo_codex_session_completed_turns{project_id="turns-zero",session_id="one"} 0',
                'cwo_codex_session_failed_turns{project_id="turns-zero",session_id="one"} 0',
                'cwo_codex_session_completed_turns{project_id="turns-partial",session_id="recorded"} 3',
                'cwo_codex_session_failed_turns{project_id="turns-partial",session_id="recorded"} 0',
            ]
        )

        # Normal primary/subagent/internal identities, plus an identity whose
        # model and kind disagree across otherwise valid info series.
        for session, kind in (
            ("primary", "session"),
            ("worker", "subagent"),
            ("guardian", "internal"),
        ):
            info("kinds", session, kind=kind)
            observed("kinds", session, 2)
        info("metadata", "stable", kind="session", model="model-a", effort="high")
        observed("metadata", "stable", 2)
        usage("metadata", "stable", 1, tokens=complete_tokens, responses=2)
        info("metadata", "ambiguous", kind="session", model="model-a", effort="high")
        info(
            "metadata",
            "ambiguous",
            kind="subagent",
            model="model-b",
            effort="low",
        )
        observed("metadata", "ambiguous", 2)
        lines.extend(
            [
                'cwo_codex_session_usage_state{project_id="metadata",session_id="ambiguous",replica="one"} 1',
                'cwo_codex_session_usage_state{project_id="metadata",session_id="ambiguous",replica="two"} 4',
            ]
        )

        # Percentage fixtures distinguish observed zero subsets from absent ratios.
        zero_subset = {
            "input": 10,
            "cached_input": 0,
            "output": 5,
            "reasoning_output": 0,
            "total": 15,
        }
        all_zero = {name: 0 for name in zero_subset}
        for project, tokens in (("ratio-zero", zero_subset), ("ratio-absent", all_zero)):
            info(project, "work")
            observed(project, "work", 2)
            usage(project, "work", 1, tokens=tokens, responses=0)

        # A small covered command population exercises the inherited expressions.
        for observation, outcome, age, duration in (
            ("ok", "completed", 20, 2),
            ("failed", "failed", 10, 4),
        ):
            labels = (
                'project_id="mixed",session_id="complete",'
                f'observation_id="{observation}",outcome="{outcome}"'
            )
            lines.append(
                f"cwo_codex_command_event_timestamp_seconds{{{labels}}} {cls.now - age}"
            )
            lines.append(f"cwo_codex_command_event_duration_seconds{{{labels}}} {duration}")
        lines.extend(
            [
                "cwo_codex_command_event_timestamp_seconds"
                f'{{project_id="mixed",session_id="orphan",observation_id="unknown",outcome="unknown"}} {cls.now - 5}',
            ]
        )

        cls.endpoint = MetricsEndpoint("127.0.0.1", 0, credential)
        cls.endpoint._payload = ("\n".join(lines) + "\n").encode("utf-8")
        cls.endpoint.start()
        cls.addClassCleanup(cls.endpoint.close)

        credential_file = root / "fixture.token"
        credential_file.write_bytes(credential)
        credential_file.chmod(0o600)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = root / "prometheus.json"
        config.write_text(
            json.dumps(
                {
                    "global": {"scrape_interval": "250ms"},
                    "scrape_configs": [
                        {
                            "job_name": "unified-fixture",
                            "scrape_interval": "250ms",
                            "scrape_timeout": "200ms",
                            "authorization": {
                                "type": "Bearer",
                                "credentials_file": str(credential_file),
                            },
                            "static_configs": [
                                {
                                    "targets": [
                                        f"127.0.0.1:{cls.endpoint.server.server_address[1]}"
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        cls.process = subprocess.Popen(
            [
                os.environ["CWO_TEST_PROMETHEUS_BINARY"],
                "--config.file=" + str(config),
                "--storage.tsdb.path=" + str(root / "tsdb"),
                f"--web.listen-address=127.0.0.1:{port}",
                "--storage.tsdb.retention.time=1h",
                "--log.level=error",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls._stop_prometheus)
        cls.address = f"http://127.0.0.1:{port}"
        cls.client = PrometheusQueryClient(cls.address)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError("temporary Prometheus exited")
            try:
                with urlopen(cls.address + "/-/ready", timeout=1) as response:
                    if response.status == 200 and cls.client.query(
                        "cwo_codex_collector_source_available", time.time()
                    ):
                        break
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("temporary Prometheus did not scrape fixture")

        cls.query_at = time.time()
        dashboard = json.loads(UNIFIED.read_text(encoding="utf-8"))
        cls.panels = panels_by_id(dashboard)

    @classmethod
    def _stop_prometheus(cls) -> None:
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    def query(
        self,
        panel_id: int,
        *,
        ref: str = "A",
        project: str = "mixed",
        session: str = ".+",
        start: int | None = None,
        end: int | None = None,
        when: float | None = None,
    ) -> list[dict]:
        start = self.now - 1800 if start is None else start
        end = self.now if end is None else end
        target = next(
            target
            for target in self.panels[panel_id]["targets"]
            if target["refId"] == ref
        )
        expression = (
            target["expr"]
            .replace("$project", project)
            .replace("$session", session)
            .replace("$__from", str(start * 1000))
            .replace("$__to", str(end * 1000))
        )
        return self.client.query(expression, self.query_at if when is None else when)

    def value(self, panel_id: int, **kwargs) -> float:
        rows = self.query(panel_id, **kwargs)
        self.assertEqual(len(rows), 1, (panel_id, kwargs, rows))
        return float(rows[0]["value"][1])

    def query_expression(
        self,
        expression: str,
        *,
        project: str,
        session: str = ".+",
        start: int | None = None,
        end: int | None = None,
        when: float | None = None,
    ) -> list[dict]:
        start = self.now - 1800 if start is None else start
        end = self.now if end is None else end
        expression = (
            expression.replace("$project", project)
            .replace("$session", session)
            .replace("$__from", str(start * 1000))
            .replace("$__to", str(end * 1000))
        )
        return self.client.query(expression, self.query_at if when is None else when)

    def by_session(self, panel_id: int, **kwargs) -> dict[str, float]:
        rows = self.query(panel_id, **kwargs)
        self.assertEqual(len(rows), len({row["metric"]["session_id"] for row in rows}))
        return {
            row["metric"]["session_id"]: float(row["value"][1]) for row in rows
        }

    def ring_counts(self, **kwargs) -> dict[str, float]:
        return {
            target["legendFormat"]: float(rows[0]["value"][1])
            for target in self.panels[23]["targets"]
            if target["refId"] in "ABCDE"
            and (rows := self.query(23, ref=target["refId"], **kwargs))
        }

    def test_matching_state_counts_share_one_complete_population(self) -> None:
        self.assertEqual(self.value(3), 4)
        self.assertEqual(self.value(4), 1)
        self.assertEqual(self.value(6), 3)
        self.assertEqual(
            self.ring_counts(), {"Working": 1, "Waiting": 3}
        )
        self.assertEqual(self.by_session(11, ref="B"), {
            "complete": 1,
            "partial": 2,
            "unknown": 2,
            "conflicted": 2,
        })

        self.assertEqual(self.value(3, project="single"), 1)
        self.assertEqual(self.value(4, project="single"), 0)
        self.assertEqual(self.value(6, project="single"), 1)
        self.assertEqual(self.ring_counts(project="single"), {"Waiting": 1})

    def test_activity_prefilter_matches_original_edge_case_semantics(self) -> None:
        original = (
            '(sum(((max by (project_id, session_id) '
            '(cwo_codex_session_state{project_id=~"$project",session_id=~"$session"}) '
            '== 1)) and on (project_id, session_id) '
            '((max by (project_id, session_id) '
            '(cwo_codex_session_last_event_timestamp_seconds{project_id=~"$project",session_id=~"$session"}) '
            '>= $__from / 1000) and (max by (project_id, session_id) '
            '(cwo_codex_session_last_event_timestamp_seconds{project_id=~"$project",session_id=~"$session"}) '
            '<= $__to / 1000))) or vector(0)) and on () '
            '(max(cwo_codex_collector_source_available) == 1) and on () '
            '((time() - max(cwo_codex_collector_scan_timestamp_seconds)) < 20)'
        )
        self.assertEqual(self.panels[21]["maxDataPoints"], 180)
        self.assertEqual(
            self.panels[21]["targets"][0]["expr"].count(
                "cwo_codex_session_last_event_timestamp_seconds"
            ),
            1,
        )

        optimized_rows = self.query(21, project="activity-edge")
        original_rows = self.query_expression(original, project="activity-edge")
        self.assertEqual(optimized_rows, original_rows)
        self.assertEqual(float(optimized_rows[0]["value"][1]), 3)

        stale = self.now + 30
        self.assertEqual(
            self.query(21, project="activity-edge", when=stale),
            self.query_expression(original, project="activity-edge", when=stale),
        )
        self.assertEqual(self.query(21, project="activity-edge", when=stale), [])

    def test_fresh_empty_is_zero_but_stale_state_is_unavailable(self) -> None:
        for panel_id in (3, 4, 6):
            self.assertEqual(self.value(panel_id, project="absent"), 0)
        self.assertEqual(self.ring_counts(project="absent"), {})
        self.assertTrue(math.isnan(self.value(23, ref="F", project="absent")))
        self.assertEqual(self.query(23, ref="G", project="absent"), [])

        stale = self.now + 30
        for panel_id in (3, 4, 6):
            self.assertEqual(self.query(panel_id, project="single", when=stale), [])
        self.assertEqual(self.ring_counts(project="single", when=stale), {})
        self.assertTrue(
            math.isnan(self.value(23, ref="G", project="single", when=stale))
        )

    def test_missing_or_conflicting_state_suppresses_partial_counts(self) -> None:
        for project in ("state-missing", "state-conflict"):
            with self.subTest(project=project):
                self.assertEqual(self.value(3, project=project), 2)
                self.assertEqual(self.query(4, project=project), [])
                self.assertEqual(self.query(6, project=project), [])
                self.assertEqual(self.ring_counts(project=project), {})
                self.assertTrue(
                    math.isnan(self.value(23, ref="G", project=project))
                )
                self.assertEqual(
                    self.by_session(11, ref="B", project=project), {"known": 2}
                )

    def test_subagent_count_accepts_internal_and_rejects_kind_conflict(self) -> None:
        self.assertEqual(self.value(3, project="kinds"), 3)
        self.assertEqual(self.value(7, project="kinds"), 1)

        self.assertEqual(self.value(3, project="metadata"), 2)
        self.assertEqual(self.query(7, project="metadata"), [])

    def test_usage_summaries_exclude_orphans_unknown_and_conflicted_sources(self) -> None:
        self.assertEqual(self.value(32), 500)
        self.assertEqual(self.value(33), 400)
        self.assertEqual(self.value(35), 100)
        self.assertEqual(self.value(37), 5)
        self.assertAlmostEqual(self.value(34), 42.5)
        self.assertAlmostEqual(self.value(36), 40)
        self.assertEqual(self.by_session(80), {"complete": 140, "partial": 360})
        self.assertEqual(
            self.by_session(81), {"complete": 55, "partial": 0, "unknown": 7}
        )

        self.assertEqual(
            self.by_session(42, ref="A"),
            {"complete": 1, "partial": 1, "unknown": 1, "conflicted": 1},
        )
        self.assertEqual(
            self.by_session(42, ref="B"), {"complete": 140, "partial": 360}
        )
        self.assertEqual(
            self.by_session(42, ref="G"), {"complete": 2, "partial": 3}
        )
        self.assertEqual(
            self.by_session(42, ref="H"),
            {"complete": 1, "partial": 4, "unknown": 0, "conflicted": 3},
        )

    def test_selected_record_coverage_partitions_matching_population(self) -> None:
        expected = {
            "A": 1,
            "B": 1,
            "C": 0,
            "D": 1,
            "E": 1,
            "F": 0,
        }
        actual = {ref: self.value(38, ref=ref) for ref in expected}
        self.assertEqual(actual, expected)
        self.assertEqual(sum(actual.values()), self.value(3))

    def test_ambiguous_usage_is_unavailable_and_coverage_is_partial(self) -> None:
        expected = {
            "A": 1,
            "B": 0,
            "C": 0,
            "D": 0,
            "E": 0,
            "F": 1,
        }
        actual = {
            ref: self.value(38, ref=ref, project="metadata") for ref in expected
        }
        self.assertEqual(actual, expected)
        self.assertEqual(sum(actual.values()), self.value(3, project="metadata"))
        self.assertEqual(self.value(39, project="metadata"), 2)

    def test_every_population_scoped_table_target_excludes_orphans(self) -> None:
        expected_sessions = {"complete", "partial", "unknown", "conflicted"}
        table_panel_ids = (11, 41, 42, 141, 143)
        for panel_id in table_panel_ids:
            targets = self.panels[panel_id]["targets"]
            self.assertEqual(
                {
                    row["metric"]["session_id"]
                    for row in self.query(panel_id, ref=targets[0]["refId"])
                },
                expected_sessions,
                panel_id,
            )
            for target in targets:
                with self.subTest(panel_id=panel_id, ref=target["refId"]):
                    rows = self.query(panel_id, ref=target["refId"])
                    self.assertTrue(
                        all("session_id" in row["metric"] for row in rows), rows
                    )
                    self.assertNotIn(
                        "orphan",
                        {row["metric"]["session_id"] for row in rows},
                        rows,
                    )

    def test_percentages_preserve_safe_zero_and_absent_denominators(self) -> None:
        for panel_id in (34, 36):
            self.assertEqual(self.value(panel_id, project="ratio-zero"), 0)
            self.assertEqual(self.query(panel_id, project="ratio-absent"), [])
        for panel_id in (32, 33, 35, 37):
            self.assertEqual(self.value(panel_id, project="ratio-absent"), 0)

    def test_metadata_conflicts_keep_one_identity_row_with_fallback_labels(self) -> None:
        identity_rows = self.query(11, ref="A", project="metadata")
        self.assertEqual(
            sorted(row["metric"]["session_id"] for row in identity_rows),
            ["ambiguous", "stable"],
        )
        model_rows = {
            row["metric"]["session_id"]: row["metric"]["model_effort"]
            for row in self.query(11, ref="M", project="metadata")
        }
        role_rows = {
            row["metric"]["session_id"]: row["metric"]["kind"]
            for row in self.query(11, ref="R", project="metadata")
        }
        self.assertEqual(
            model_rows, {"stable": "model-a · high", "ambiguous": "Not observed"}
        )
        self.assertEqual(
            role_rows, {"stable": "session", "ambiguous": "unavailable"}
        )

    def test_inherited_command_queries_keep_event_time_bounds(self) -> None:
        self.assertEqual(self.value(51), 3)
        self.assertEqual(self.value(52), 1)
        self.assertEqual(self.value(54), 4)

    def test_local_coverage_preserves_complete_incomplete_and_unavailable(self) -> None:
        self.assertEqual(
            self.panels[158]["targets"], self.panels[159]["targets"]
        )
        for panel_id in (158, 159):
            with self.subTest(panel_id=panel_id, coverage="complete"):
                self.assertEqual(self.value(panel_id), 1)
            with self.subTest(panel_id=panel_id, coverage="incomplete"):
                self.assertEqual(
                    self.value(panel_id, start=self.now - 700000), 0
                )
            with self.subTest(panel_id=panel_id, coverage="unavailable"):
                self.assertEqual(self.value(panel_id, when=self.now + 30), -1)

    def test_recorded_turn_sums_preserve_missing_zero_and_lower_bounds(self) -> None:
        self.assertEqual(self.value(201, ref="A", project="turns-recorded"), 5)
        self.assertEqual(self.value(201, ref="B", project="turns-recorded"), 1)

        self.assertEqual(self.value(201, ref="A", project="turns-zero"), 0)
        self.assertEqual(self.value(201, ref="B", project="turns-zero"), 0)

        self.assertEqual(self.query(201, ref="A", project="turns-missing"), [])
        self.assertEqual(self.query(201, ref="B", project="turns-missing"), [])

        incomplete_start = self.now - 700000
        self.assertEqual(
            self.value(
                158,
                project="turns-partial",
                start=incomplete_start,
            ),
            0,
        )
        self.assertEqual(
            self.value(
                201,
                ref="A",
                project="turns-partial",
                start=incomplete_start,
            ),
            3,
        )
        self.assertEqual(
            self.value(
                201,
                ref="B",
                project="turns-partial",
                start=incomplete_start,
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
