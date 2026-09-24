"""Evaluate the shipped audit queries with a separately verified Prometheus tool."""
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
    def test_source_time_boundaries_dedup_backfill_and_health(self):
        dashboard = json.loads((ROOT / "examples/observability/cwo-observed-dispatches.json").read_text())
        queries = {p["id"]: p["targets"][0]["expr"].replace("$__range", "1h").replace("$__from", "3600000").replace("$__to", "7200000")
                   for p in dashboard["panels"] if p.get("targets") and p["id"] >= 200}
        def series(name, value, samples=120):
            return {"series": name, "values": f"{value}x{samples}"}
        def event(identity, kind, timestamp, instance="one"):
            return series(f'cwo_audit_event_timestamp_seconds{{event_id="{identity}",event_type="{kind}",instance="{instance}"}}', timestamp)
        sources = [event("old", "packet_built", 3599), event("start", "packet_built", 3600),
                   event("middle", "dispatch_prepared", 5400), event("end", "packet_built", 7200),
                   event("future", "return_evaluated", 7201), event("middle", "dispatch_prepared", 5400, "copy")]
        def checks(values):
            return [{"expr": queries[key], "eval_time": "2h", "exp_samples": [] if value is None else [{"labels": "{}", "value": value}]}
                    for key, value in values.items()]
        fixtures = [
            {"input_series": sources + [series("cwo_audit_collection_complete", 1)],
             "promql_expr_test": checks({201: 3, 202: 2, 203: 1, 204: 0, 206: 1})},
            {"input_series": sources + [series("cwo_audit_collection_complete", 0)],
             "promql_expr_test": checks({201: 3, 204: None, 206: 0})},
            {"input_series": [series("cwo_audit_collection_complete", 1)],
             "promql_expr_test": checks({201: 0, 202: 0, 203: 0, 204: 0})},
            {"input_series": [series('cwo_audit_collection_complete{instance="one"}', 1), series('cwo_audit_collection_complete{instance="two"}', 0)],
             "promql_expr_test": checks({201: None, 206: 0})},
            {"input_series": [], "promql_expr_test": checks({201: None, 206: None})},
            {"input_series": [series("cwo_audit_collection_complete", 1, samples=118)],
             "promql_expr_test": checks({201: None, 206: None})},
        ]
        for case in fixtures:
            case["interval"] = "1m"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit-rules.json"
            path.write_text(json.dumps({"rule_files": [], "evaluation_interval": "1m", "tests": fixtures}))
            result = subprocess.run([str(Path(BINARY).with_name("promtool")), "test", "rules", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
