"""Evaluate shipped workflow bars and audit health against source-time fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("CWO_TEST_PROMETHEUS_BINARY")


@unittest.skipUnless(BINARY, "separately verified Prometheus binary not supplied")
class AuditQueryTests(unittest.TestCase):
    def test_source_time_boundaries_dedup_partial_and_empty(self):
        dashboard = json.loads((ROOT / "examples/observability/cwo-overview.json").read_text())
        panels = {p["id"]: p for p in dashboard["panels"]}
        def query(identity, ref="A"):
            raw = next(t["expr"] for t in panels[identity]["targets"] if t["refId"] == ref)
            return raw.replace("$__range", "1h").replace("$__from", "3600000").replace("$__to", "7200000")
        def series(name, value, samples=120):
            return {"series": name, "values": f"{value}x{samples}"}
        def event(identity, kind, timestamp, instance="one"):
            return series(f'cwo_audit_event_timestamp_seconds{{event_id="{identity}",event_type="{kind}",instance="{instance}"}}', timestamp)
        sources = [event("old", "packet_built", 3599), event("start", "packet_built", 3600),
                   event("middle", "dispatch_prepared", 5400), event("end", "packet_built", 7200),
                   event("future", "return_evaluated", 7201), event("middle", "dispatch_prepared", 5400, "copy")]
        def checks(bars, health):
            return [
                {"expr": query(205), "eval_time": "2h", "exp_samples": [
                    {"labels": '{event_type="' + kind + '"}', "value": value} for kind, value in bars.items()]},
                {"expr": query(207, "B"), "eval_time": "2h", "exp_samples": [] if health is None else [{"labels": "{}", "value": health}]},
            ]
        fixtures = []
        for coverage in (0, 1):
            fixtures.append({"input_series": sources + [series("cwo_audit_collection_complete", coverage)],
                             "promql_expr_test": checks({"packet_built": 2, "dispatch_prepared": 1}, coverage)})
            fixtures.append({"input_series": [series("cwo_audit_collection_complete", coverage)],
                             "promql_expr_test": checks({}, coverage)})
        fixtures.extend([
            {"input_series": [], "promql_expr_test": checks({}, None)},
            {"input_series": [series("cwo_audit_collection_complete", 1, 118)], "promql_expr_test": checks({}, None)},
            {"input_series": [series('cwo_audit_collection_complete{instance="one"}', 1), series('cwo_audit_collection_complete{instance="two"}', 0)],
             "promql_expr_test": checks({}, 0)},
        ])
        for case in fixtures:
            case["interval"] = "1m"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit-rules.json"
            path.write_text(json.dumps({"rule_files": [], "evaluation_interval": "1m", "tests": fixtures}))
            result = subprocess.run([str(Path(BINARY).with_name("promtool")), "test", "rules", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
