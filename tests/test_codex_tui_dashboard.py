"""Guard the experimental layout, existing-metric semantics and portable rendering."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_release import build_release
from render_codex_sessions_dashboard import render_dashboard
from test_codex_beta_renderer import snapshot
from test_codex_beta_dashboard import ALLOWED_METRICS, walk_panels
import test_codex_reliability_queries as reliability


def template():
    return json.loads((ROOT / "examples/observability/codex-tui-beta.json").read_text())


class TuiDashboardTests(unittest.TestCase):
    def test_separate_identity_and_six_sections(self):
        dashboard = template()
        self.assertEqual(dashboard["uid"], "traceonaut-codex-tui-beta")
        self.assertEqual(dashboard["title"], "Codex TUI · Beta")
        self.assertEqual(dashboard["refresh"], "30s")
        self.assertEqual(dashboard["time"], {"from": "now-24h", "to": "now"})
        panels = sorted(dashboard["panels"], key=lambda panel: panel["id"])
        self.assertEqual([p["id"] for p in panels], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([p["title"].split(" · ")[0] for p in panels], ["1", "2", "3", "4", "5", "6", "2"])
        expected_positions = {
            1: (0, 0, 12, 8), 2: (12, 0, 6, 8), 7: (18, 0, 6, 8),
            3: (0, 8, 12, 8), 4: (12, 8, 12, 8),
            5: (0, 16, 12, 10), 6: (12, 16, 12, 10),
        }
        for panel in panels:
            self.assertEqual(panel["gridPos"], dict(zip(("x", "y", "w", "h"), expected_positions[panel["id"]])))
            self.assertIn(panel["type"], {"stat", "timeseries", "bargauge", "table"})
            self.assertFalse(panel["transparent"])
            self.assertIn("reading-values/", panel["description"])

    def test_only_existing_session_metrics_and_correct_units(self):
        for panel in template()["panels"]:
            for target in panel["targets"]:
                metrics = set(re.findall(r"\b(cwo_[a-z0-9_]+)\b", target["expr"]))
                self.assertTrue(metrics)
                self.assertLessEqual(metrics, ALLOWED_METRICS)
                self.assertFalse(any(m.startswith("cwo_codex_account_") for m in metrics))
                self.assertEqual(target["datasource"]["uid"], "${DS_PROMETHEUS}")
                self.assertNotRegex(target["expr"], r"\b(?:rate|increase)\(")
        charts = {panel["id"]: panel for panel in template()["panels"]}
        input_chart, output_chart = charts[2], charts[7]
        self.assertIn('token_kind="input"', input_chart["targets"][0]["expr"])
        self.assertIn('token_kind="output"', output_chart["targets"][0]["expr"])
        self.assertIn("not tokens spent", input_chart["description"])
        self.assertIn("not tokens spent", output_chart["description"])
        self.assertNotIn('"value": "right"', json.dumps(output_chart["fieldConfig"]))
        self.assertIn("distinct from user messages", charts[3]["description"])
        self.assertIn("topk(8,", charts[3]["targets"][0]["expr"])
        self.assertEqual(input_chart["fieldConfig"]["defaults"]["unit"], "short")
        self.assertFalse(any("Token scale" in x.get("title", "") for x in template().get("links", [])))

    def test_reuses_validated_queries_and_preserves_partial_counts(self):
        old = json.loads((ROOT / "examples/observability/codex-work-overview-beta.json").read_text())
        old = {p["id"]: p for p in walk_panels(old["panels"])}
        new = {panel["id"]: panel for panel in template()["panels"]}
        for target, source in zip(new[1]["targets"], [2, 101, 4, 6, 7, 32]):
            self.assertEqual(target["expr"], old[source]["targets"][0]["expr"])
        for target, source in zip([new[2]["targets"][0], new[7]["targets"][0]], [33, 35]):
            self.assertTrue(target["expr"].startswith("(" + old[source]["targets"][0]["expr"] + ")"))
            self.assertIn("time() - max(cwo_codex_collector_scan_timestamp_seconds)", target["expr"])
            self.assertTrue(target["range"])
            self.assertFalse(target["instant"])
        self.assertEqual([t["expr"] for t in new[4]["targets"]],
                         [t["expr"] for source in [51, 52, 58] for t in old[source]["targets"]])
        partial = {o["matcher"]["options"]: o["properties"] for o in new[4]["fieldConfig"]["overrides"]}
        for label in ["Commands · Partial", "Failures · Partial"]:
            self.assertIn({"id": "unit", "value": "prefix:≥ "}, partial[label])
        self.assertEqual(new[5]["targets"][1]["expr"],
                         old[21]["targets"][0]["expr"].replace("== 1)", "== 2)", 1)
                         .replace("(sum(", "(count(", 1))

    def test_compact_table_and_protected_name_projection(self):
        source = snapshot()
        source["sessions"][0]["prompt"] = "private content excluded"
        original = copy.deepcopy(source)
        rendered = render_dashboard(template(), source, "test-prometheus")
        self.assertEqual(source, original)
        table = next(panel for panel in rendered["panels"] if panel["id"] == 6)
        self.assertGreaterEqual(len(table["targets"]), 2)
        self.assertEqual(list(table["transformations"][2]["options"]["renameByName"].values()),
                         ["Chat", "State", "Model / effort", "Tokens"])
        widths = [p["value"] for o in table["fieldConfig"]["overrides"]
                  for p in o["properties"] if p["id"] == "custom.width"]
        self.assertLessEqual(sum(widths), 450)
        names = next(o for o in table["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Chat")
        mapping = next(p["value"] for p in names["properties"] if p["id"] == "mappings")
        self.assertEqual(mapping[0]["options"]["parent"]["text"], "Build dashboard")
        links = next(p["value"] for p in names["properties"] if p["id"] == "links")
        self.assertIn("${__data.fields.session_id:percentencode}", links[0]["url"])
        hidden = next(o for o in table["fieldConfig"]["overrides"] if o["matcher"]["options"] == "session_id")
        self.assertIn({"id": "custom.hidden", "value": True}, hidden["properties"])
        text = json.dumps(rendered)
        self.assertNotIn("private content excluded", text)
        self.assertNotIn("${DS_PROMETHEUS}", text)
        self.assertNotIn("__inputs", rendered)

    def test_standalone_release_renders_without_other_templates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = build_release("tui-beta", root / "releases")
            source = root / "sessions.json"
            source.write_text(json.dumps(snapshot()))
            source.chmod(0o600)
            output = root / "tui.json"
            result = subprocess.run([sys.executable,
                str(release / "scripts/render_codex_sessions_dashboard.py"),
                "--template", str(release / "examples/observability/codex-tui-beta.json"),
                "--snapshot-file", str(source), "--output", str(output)],
                cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(output.read_text())["uid"], "traceonaut-codex-tui-beta")
            self.assertIn("Build dashboard", output.read_text())


# Use the existing independent Prometheus fixture, not a dashboard-produced fixture.
@unittest.skipUnless(reliability.os.environ.get("CWO_TEST_PROMETHEUS_BINARY"),
                     "separately verified Prometheus binary not supplied")
class TuiQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reliability.CommandQueryIntegrationTests.setUpClass.__func__(cls)
        cls.panel_ids = {p["id"]: p for p in template()["panels"]}

    query = reliability.CommandQueryIntegrationTests.query
    value = reliability.CommandQueryIntegrationTests.value

    def test_all_queries_parse_and_execute(self):
        for panel in template()["panels"]:
            for target in panel["targets"]:
                with self.subTest(panel=panel["id"], ref=target["refId"]):
                    self.query(panel["id"], ref=target["refId"])

    def test_filters_and_state_counts(self):
        self.assertEqual(self.value(1, ref="C"), 0)
        self.assertEqual(self.value(1, ref="D"), 4)
        self.assertEqual(self.value(5, ref="B", session="work-a"), 1)
        self.assertEqual(self.value(4, ref="A"), 5)
        self.assertEqual(self.value(4, ref="C"), 1)
        self.assertEqual(self.value(2, ref="A"), 400)
        self.assertEqual(self.value(7, ref="A"), 100)

    def test_stale_collection_does_not_show_healthy_values(self):
        for panel, ref in [(1, "C"), (2, "A"), (4, "A"), (5, "B"), (6, "B"), (7, "A")]:
            with self.subTest(panel=panel):
                self.assertEqual(self.query(panel, ref=ref, when=self.query_at + 25), [])


if __name__ == "__main__":
    unittest.main()
