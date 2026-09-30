"""The shipped CWO dashboard uses current file collectors, not a job launcher."""
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_observability_dashboard import render_dashboard, walk_panels
from traceonaut.cwo_audit_telemetry import METRICS as AUDIT
from traceonaut.cwo_review_telemetry import METRICS as REVIEW
from traceonaut.cwo_session_telemetry import METRICS as CWO


class CurrentCwoDashboardTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads((ROOT / "examples/observability/cwo-overview.json").read_text())
        self.panels = {p["id"]: p for p in walk_panels(self.dashboard["panels"])}

    def test_identity_refresh_and_no_orphaned_legacy_panels_or_filters(self):
        self.assertEqual(self.dashboard["uid"], "cwo-dispatch-observability-v1")
        self.assertEqual(self.dashboard["title"], "CWO Overview")
        self.assertEqual(self.dashboard["refresh"], "1m")
        variables = self.dashboard["templating"]["list"]
        self.assertEqual([(v["name"], v["label"]) for v in variables],
                         [("project", "Project"), ("session", "Session")])
        for variable in variables:
            self.assertEqual(variable["hide"], 0)
            self.assertTrue(variable["multi"] and variable["includeAll"])
            self.assertEqual(variable["allValue"], ".*")
        text = json.dumps(self.dashboard)
        for obsolete in ("cwo_dispatch_", "cwo_cycle_", "cwo_telemetry_", "existing ledger", "observation hook", "$dispatch"):
            self.assertNotIn(obsolete, text)
        self.assertNotIn(150, self.panels)
        self.assertNotIn(90, self.panels)
        self.assertNotIn(500, self.panels)
        self.assertNotIn("cwo_pool_", text)

    def test_only_supported_metrics_and_native_panels(self):
        session = {"cwo_codex_session_" + name for name in (
            "info", "state", "usage_tokens", "usage_state", "last_event_timestamp_seconds",
            "completed_turns", "failed_turns", "observed_turn_seconds")}
        allowed = set(AUDIT) | set(REVIEW) | set(CWO) | session
        for panel in self.panels.values():
            self.assertNotIn(panel["type"], ("text", "piechart"))
            for target in panel.get("targets", []):
                names = set(re.findall(r"\b(cwo_[a-z0-9_]+)\b", target["expr"]))
                self.assertTrue(names)
                self.assertLessEqual(names, allowed)
                self.assertEqual(target["datasource"]["uid"], "${DS_PROMETHEUS}")

    def test_outcome_and_time_use_existing_session_metrics_and_range_population(self):
        table = self.panels[305]
        queries = {t["refId"]: t["expr"] for t in table["targets"]}
        for ref, metric in (("E", "completed_turns"), ("F", "failed_turns"), ("G", "observed_turn_seconds")):
            self.assertIn("cwo_codex_session_" + metric, queries[ref])
            self.assertIn("cwo_codex_session_cwo_association_timestamp_seconds", queries[ref])
            self.assertIn("$__from / 1000", queries[ref])
            self.assertIn("$__to / 1000", queries[ref])
            self.assertNotIn("vector(0)", queries[ref])
        titles = set(table["transformations"][-1]["options"]["renameByName"].values())
        self.assertTrue({"Turns done", "Turns failed", "Turn time", "Tokens", "Model / effort"} <= titles)
        self.assertIn("not assignment wall time", table["description"])
        widths = [v["value"] for o in table["fieldConfig"]["overrides"] for v in o["properties"] if v["id"] == "custom.width"]
        self.assertLessEqual(sum(widths) + 70 * (len(titles) - len(widths)), 1700)

    def test_renderer_keeps_current_template_free_of_legacy_filters(self):
        rendered = render_dashboard(self.dashboard, {"projects": {}, "dispatches": {}}, datasource_uid="example")
        self.assertEqual([v["name"] for v in rendered["templating"]["list"]], ["project", "session"])
        self.assertNotIn("${DS_PROMETHEUS}", json.dumps(rendered))
        self.assertNotIn("__inputs", rendered)

    def test_filters_cover_session_helper_and_review_panels(self):
        for identity in (301, 302, 303, 304, 305, 310, 401):
            for target in self.panels[identity]["targets"]:
                self.assertIn('project_id=~"${project:regex}"', target["expr"])
                self.assertIn('session_id=~"${session:regex}"', target["expr"])
        for identity in (309, 207, 205):
            for target in self.panels[identity]["targets"]:
                self.assertNotIn("${project", target["expr"])
                self.assertNotIn("${session", target["expr"])
        self.assertIn("all projects", self.panels[205]["title"])
        for identity in (309, 207):
            self.assertIn("Profile-wide", self.panels[identity]["description"])
