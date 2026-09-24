"""Exercise session-dashboard PromQL against isolated session fixtures."""

from __future__ import annotations

import json
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
ALL_SESSIONS = OBSERVABILITY / "codex-all-sessions.json"


def panels_by_id(dashboard: dict) -> dict[int, dict]:
    result: dict[int, dict] = {}

    def visit(panels: list[dict]) -> None:
        for panel in panels:
            result[panel["id"]] = panel
            visit(panel.get("panels", []))

    visit(dashboard["panels"])
    return result


@unittest.skipUnless(
    os.environ.get("CWO_TEST_PROMETHEUS_BINARY"),
    "separately verified Prometheus binary not supplied",
)
class AllSessionsQueryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.now = int(time.time())
        credential = b"synthetic-sessions-fixture"
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
        for session, total in (("complete", 140), ("partial", 360), ("unknown", 11), ("conflicted", 99)):
            lines.append(
                'cwo_codex_session_reported_tokens'
                f'{{project_id="mixed",session_id="{session}"}} {total}'
            )
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
                            "job_name": "sessions-fixture",
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

    @classmethod
    def _stop_prometheus(cls) -> None:
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)


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


    def test_all_sessions_combines_selected_settings_without_changing_selection(self) -> None:
        panels = panels_by_id(json.loads(ALL_SESSIONS.read_text()))
        identity = next(t["expr"] for t in panels[110]["targets"] if t["refId"] == "A")
        rows = self.query_expression(identity, project="mixed")
        self.assertEqual({row["metric"]["session_id"] for row in rows},
                         {"complete", "partial", "unknown", "conflicted"})
        for row in rows:
            self.assertEqual(row["metric"]["model_effort"], "model-a · medium")
            self.assertNotIn("model", row["metric"])
            self.assertNotIn("effort", row["metric"])
        rows = self.query_expression(identity, project="mixed", session="partial")
        self.assertEqual([row["metric"]["session_id"] for row in rows], ["partial"])
        self.assertEqual(self.query_expression(identity, project="mixed", start=self.now - 5), [])
        self.assertEqual(self.query_expression(identity, project="missing"), [])

    def test_all_sessions_diagnostics_preserve_runtime_values_and_coverage(self) -> None:
        panels = panels_by_id(json.loads(ALL_SESSIONS.read_text()))
        targets = {t["refId"]: t["expr"] for t in panels[173]["targets"]}
        for ref, expected in (
            ("D", {"complete": 140, "partial": 360, "unknown": 11, "conflicted": 99}),
            ("J", {"complete": 1, "partial": 4, "unknown": 0, "conflicted": 3}),
        ):
            with self.subTest(ref=ref):
                rows = self.query_expression(targets[ref], project="mixed", session="complete|partial|unknown|conflicted")
                self.assertEqual({r["metric"]["session_id"]: float(r["value"][1]) for r in rows}, expected)
                self.assertEqual(self.query_expression(targets[ref], project="mixed", start=self.now - 5), [])
        recorded = next(t["expr"] for t in panels[110]["targets"] if t["refId"] == "C")
        rows = self.query_expression(recorded, project="mixed", session="complete|partial|unknown|conflicted")
        self.assertEqual({r["metric"]["session_id"]: float(r["value"][1]) for r in rows},
                         {"complete": 140, "partial": 360})


if __name__ == "__main__":
    unittest.main()
