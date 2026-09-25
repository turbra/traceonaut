"""Response timestamps stay distinct from monotonic lifecycle timing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_observability_dashboard import walk_panels

BINARY = os.environ.get("CWO_TEST_PROMETHEUS_BINARY")


@unittest.skipUnless(BINARY, "separately verified Prometheus binary not supplied")
class LastResponseTests(unittest.TestCase):
    def test_latest_response_and_response_less_jobs(self):
        data = json.loads((ROOT / "examples/observability/cwo-overview.json").read_text())
        panels = {p["id"]: p for p in walk_panels(data["panels"])}
        work = panels[110]
        self.assertEqual(work["transformations"][0]["options"], {"byField": "dispatch_id", "mode": "outer"})
        self.assertIn("Last response", work["transformations"][2]["options"]["renameByName"].values())
        self.assertNotIn("Finished", work["transformations"][2]["options"]["renameByName"].values())
        def expr(panel, ref="A", project="p", dispatch=".+"):
            raw = next(t["expr"] for t in panels[panel]["targets"] if t["refId"] == ref)
            return raw.replace("$project", project).replace("$dispatch", dispatch).replace("$__range", "1h")
        def series(metric, labels, value):
            return {"series": metric + "{" + labels + "}", "values": str(value) + "x120"}
        source = [
            series("cwo_dispatch_info", 'project_id="p",dispatch_id="responded",requested_model="m",requested_effort="high"', 1),
            series("cwo_dispatch_info", 'project_id="p",dispatch_id="no-response",requested_model="m",requested_effort="high"', 1),
            series("cwo_cycle_observed_timestamp_seconds", 'project_id="p",dispatch_id="responded",cycle_ordinal="1"', 5000),
            series("cwo_cycle_observed_timestamp_seconds", 'project_id="p",dispatch_id="responded",cycle_ordinal="2"', 6000),
            series("cwo_cycle_observed_timestamp_seconds", 'project_id="other",dispatch_id="responded",cycle_ordinal="1"', 7000),
            series("cwo_dispatch_state", 'project_id="p",dispatch_id="responded",replica="one"', 3),
            series("cwo_dispatch_state", 'project_id="p",dispatch_id="responded",replica="two"', 3),
        ]
        checks = [
            {"expr": expr(110, "F"), "eval_time": "2h", "exp_samples": [{"labels": '{project_id="p",dispatch_id="responded"}', "value": 6000000}]},
            {"expr": expr(110, "F", dispatch="no-response"), "eval_time": "2h", "exp_samples": []},
            {"expr": "count(" + expr(110, "A") + ")", "eval_time": "2h", "exp_samples": [{"labels": "{}", "value": 2}]},
            {"expr": expr(102), "eval_time": "2h", "exp_samples": [{"labels": "{}", "value": 1}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"rule_files": [], "evaluation_interval": "1m", "tests": [{"interval": "1m", "input_series": source, "promql_expr_test": checks}]}))
            result = subprocess.run([str(Path(BINARY).with_name("promtool")), "test", "rules", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
