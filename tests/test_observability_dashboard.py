from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_contract import METRIC_FAMILIES  # noqa: E402
from traceonaut.cwo_audit_telemetry import METRICS as AUDIT_METRICS
from traceonaut.cwo_review_telemetry import METRICS as REVIEW_METRICS
from traceonaut.cwo_session_telemetry import METRICS as CWO_SESSION_METRICS
from render_observability_dashboard import walk_panels  # noqa: E402


DASHBOARD_PATH = ROOT / "examples" / "observability" / "cwo-overview.json"
SCRAPE_PATH = ROOT / "examples" / "observability" / "prometheus-scrape.yaml"


class ObservabilityDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        cls.scrape = SCRAPE_PATH.read_text(encoding="utf-8")

    def panel(self, title: str) -> dict:
        return next(
            panel for panel in walk_panels(self.dashboard["panels"]) if panel["title"] == title
        )

    @staticmethod
    def expressions(panel: dict) -> list[str]:
        return [target["expr"] for target in panel.get("targets", [])]

    def test_dashboard_is_portable_classic_json_with_one_minute_refresh(self) -> None:
        self.assertEqual(self.dashboard["uid"], "cwo-dispatch-observability-v1")
        self.assertEqual(self.dashboard["title"], "CWO Overview")
        self.assertIn("CWO-associated Codex sessions", self.dashboard["description"])
        self.assertEqual(self.dashboard["time"], {"from": "now-30d", "to": "now"})
        for other in (
            "codex-all-sessions.json",
            "codex-work-overview-beta.json",
        ):
            session = json.loads((DASHBOARD_PATH.parent / other).read_text())
            self.assertNotEqual(self.dashboard["uid"], session["uid"])
        self.assertEqual(self.dashboard["refresh"], "1m")
        self.assertEqual(self.dashboard["schemaVersion"], 39)
        self.assertIn("1y", self.dashboard["timepicker"]["time_options"])
        self.assertNotIn("scenes", self.dashboard)
        self.assertEqual(self.dashboard["__inputs"][0]["name"], "DS_PROMETHEUS")
        self.assertEqual(self.dashboard["__inputs"][0]["pluginId"], "prometheus")
        project = self.dashboard["templating"]["list"][0]
        self.assertEqual(
            project["query"]["query"],
            "label_values(cwo_telemetry_component_state, project_id)",
        )
        for panel in walk_panels(self.dashboard["panels"]):
            if panel["type"] in {"row", "text"}:
                continue
            self.assertEqual(panel["datasource"]["uid"], "${DS_PROMETHEUS}")
            self.assertIn(panel["fieldConfig"]["defaults"]["noValue"], {"—"})
            for target in panel.get("targets", []):
                self.assertEqual(target["datasource"]["uid"], "${DS_PROMETHEUS}")

    def test_all_panel_queries_use_the_frozen_metric_inventory(self) -> None:
        metric_pattern = re.compile(r"\b(cwo_[a-z0-9_]+)\b")
        observed = set()
        for panel in walk_panels(self.dashboard["panels"]):
            for expression in self.expressions(panel):
                metrics = set(metric_pattern.findall(expression))
                self.assertTrue(metrics, expression)
                observed.update(metrics)
                if panel["id"] == 401:
                    self.assertLessEqual(metrics, set(REVIEW_METRICS), expression)
                    self.assertIn("$__from / 1000", expression)
                    self.assertIn("$__to / 1000", expression)
                    self.assertNotIn("$project", expression)
                    self.assertNotIn("$dispatch", expression)
                    continue
                if panel["id"] >= 300:
                    native = {"cwo_codex_session_info", "cwo_codex_session_last_event_timestamp_seconds", "cwo_codex_session_usage_tokens", "cwo_codex_session_usage_state", "cwo_codex_session_state"}
                    self.assertLessEqual(metrics, set(CWO_SESSION_METRICS) | native, expression)
                    self.assertNotIn("$project", expression)
                    self.assertNotIn("$dispatch", expression)
                    if "cwo_codex_session_info" in metrics or "cwo_codex_session_usage_tokens" in metrics:
                        self.assertIn("cwo_codex_session_cwo_association_timestamp_seconds", expression)
                        self.assertIn("$__from / 1000", expression)
                        self.assertIn("$__to / 1000", expression)
                    continue
                if panel["id"] >= 200:
                    self.assertLessEqual(metrics, set(AUDIT_METRICS) | (set(REVIEW_METRICS) if panel["id"] == 207 else set()), expression)
                    self.assertIn("last_over_time(", expression)
                    self.assertNotIn("$project", expression)
                    self.assertNotIn("$dispatch", expression)
                    if "cwo_audit_event_timestamp_seconds" in metrics:
                        self.assertIn("max by (event_id, event_type)", expression)
                        self.assertIn("$__from / 1000", expression)
                        self.assertIn("$__to / 1000", expression)
                    continue
                self.assertLessEqual(metrics, set(METRIC_FAMILIES), expression)
                self.assertIn("last_over_time(", expression)
                if panel["id"] == 130:
                    self.assertIn("[20s]", expression)
                    self.assertNotIn("[$__range]", expression)
                else:
                    self.assertIn("[$__range]", expression)
        self.assertIn("cwo_dispatch_state", observed)
        self.assertIn("cwo_dispatch_field_state", observed)
        self.assertIn("cwo_dispatch_coverage_state", observed)
        self.assertIn("cwo_telemetry_component_state", observed)

    def test_stale_elapsed_value_is_suppressed_by_latest_field_state(self) -> None:
        panel = self.panel("Elapsed allowance and enforced runtime ceiling")
        elapsed = panel["targets"][0]["expr"]

        self.assertIn("last_over_time(cwo_dispatch_elapsed_seconds", elapsed)
        self.assertIn("and on (project_id, dispatch_id)", elapsed)
        self.assertIn(
            'last_over_time(cwo_dispatch_field_state{project_id=~"$project",dispatch_id=~"$dispatch",field="dispatch_elapsed"}[$__range]) == 1',
            elapsed,
        )
        self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "—")

    def test_stale_token_totals_require_latest_state_and_nonconflicting_coverage(
        self,
    ) -> None:
        panel = self.panel("Observed token totals with provenance")
        tokens = panel["targets"][0]["expr"]

        self.assertIn("last_over_time(cwo_dispatch_observed_tokens", tokens)
        self.assertIn("and on (project_id, dispatch_id, token_kind)", tokens)
        for allowed_state in (1, 2, 4):
            self.assertIn(f"[$__range]) == {allowed_state}", tokens)
        self.assertIn("unless on (project_id, dispatch_id)", tokens)
        self.assertIn("last_over_time(cwo_dispatch_coverage_state", tokens)
        self.assertIn("[$__range]) == 3", tokens)

        serialized = json.dumps(panel)
        self.assertIn("Runtime-normalized; upstream presence unknown", serialized)
        self.assertIn('"2"', serialized)

    def test_allowance_queries_do_not_mix_declared_and_enforced_units(self) -> None:
        cycle_panel = self.panel("Cycle allowance and enforced tool ceiling")
        elapsed_panel = self.panel("Elapsed allowance and enforced runtime ceiling")
        cycle_queries = "\n".join(self.expressions(cycle_panel))
        elapsed_queries = "\n".join(self.expressions(elapsed_panel))

        self.assertIn("cwo_dispatch_declared_cycle_allowance", cycle_queries)
        self.assertIn("cwo_dispatch_enforced_tool_call_limit", cycle_queries)
        self.assertNotIn("cwo_dispatch_enforced_runtime_limit_seconds", cycle_queries)
        self.assertIn(
            "cwo_dispatch_declared_elapsed_allowance_seconds", elapsed_queries
        )
        self.assertIn("cwo_dispatch_enforced_runtime_limit_seconds", elapsed_queries)
        self.assertNotIn("cwo_dispatch_enforced_tool_call_limit", elapsed_queries)

        remaining = cycle_panel["targets"][1]["expr"]
        overrun = cycle_panel["targets"][2]["expr"]
        self.assertIn("cwo_dispatch_coverage_state", remaining)
        self.assertIn("[$__range]) == 1", remaining)
        self.assertIn("cwo_dispatch_coverage_state", overrun)
        self.assertIn("[$__range]) == 1", overrun)
        self.assertNotIn("[$__range]) == 2", overrun)
        self.assertEqual(
            cycle_panel["targets"][2]["legendFormat"], "Exact cycle overrun"
        )

        for target in elapsed_panel["targets"]:
            if (
                "_remaining_seconds" not in target["expr"]
                and "_overrun_seconds" not in target["expr"]
            ):
                continue
            self.assertIn('field="dispatch_elapsed"}[$__range]) == 1', target["expr"])

    def test_agent_and_dispatch_counts_use_distinct_identities(self):
        active = self.panel("Agents working")["targets"][0]["expr"]
        terminal = self.panel("Completed tasks")["targets"][0]["expr"]
        self.assertIn("count by (project_id, agent_id)", active)
        self.assertIn("cwo_dispatch_state", active)
        self.assertIn("<= 2", active)
        self.assertIn("cwo_dispatch_state", terminal)
        self.assertIn("== 3", terminal)


    def test_requested_and_configured_views_are_separate_and_claim_no_attribution(
        self,
    ) -> None:
        requested = self.panel("Requested and configured models")
        configured = {"targets": requested["targets"][1:]}

        self.assertEqual(
            {"cwo_dispatch_info"},
            set(re.findall(r"\b(cwo_[a-z0-9_]+)\b", requested["targets"][0]["expr"])),
        )
        self.assertEqual(
            {
                "cwo_dispatch_configured_model_info",
                "cwo_dispatch_configured_effort_info",
                "cwo_dispatch_field_state",
            },
            {
                metric
                for expression in self.expressions(configured)
                for metric in re.findall(r"\b(cwo_[a-z0-9_]+)\b", expression)
            },
        )
        self.assertIn(
            'field="configured_model"}[$__range]) == 1',
            configured["targets"][0]["expr"],
        )
        self.assertIn(
            'field="configured_effort"}[$__range]) == 1',
            configured["targets"][1]["expr"],
        )
        serialized = json.dumps(self.dashboard).lower()
        for unsupported in (
            "actual_model",
            "actual_effort",
            "agent_model_calls",
            "per_response_duration_seconds",
            "tool_response_link",
        ):
            self.assertNotIn(unsupported, serialized)
        self.assertNotIn("estimated_completion", serialized)

    def test_default_view_joins_work_and_hides_technical_diagnostics(self) -> None:
        overview = self.dashboard["panels"]
        details = next(panel for panel in overview if panel["id"] == 90)
        self.assertTrue(details["collapsed"])
        self.assertEqual(len([p for p in details["panels"] if p["type"] == "table"]), 4)
        self.assertTrue(all(panel["id"] >= 100 or panel["id"] == 3 for panel in overview if panel["type"] != "row"))
        work = self.panel("Work and results")
        # Each query returns at most one row per dispatch. Grafana 11.5's
        # outerTabular mode combines pairs of frames into duplicate rows.
        self.assertEqual(work["transformations"][0]["options"]["mode"], "outer")
        shown = work["transformations"][1]["options"]["include"]["names"]
        for technical_field in ("Time", "job", "instance", "agent_id", "project_id", "__name__"):
            self.assertNotIn(technical_field, shown)
        fields = work["transformations"][2]["options"]["renameByName"].values()
        self.assertEqual(set(fields), {"Task", "Worker", "Status", "Model", "Effort", "Elapsed", "Responses", "Tokens", "Last response"})
        task_field = next(o for o in work["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Task")
        links = next(p["value"] for p in task_field["properties"] if p["id"] == "links")
        self.assertTrue(links[0]["url"].startswith("/d/" + self.dashboard["uid"] + "?"))
        self.assertIn("var-dispatch=${__value.raw}", links[0]["url"])
        self.assertIn("from=${__from}&to=${__to}", links[0]["url"])
        # Coverage has no token_kind label. Filtering it would silently disable
        # conflict suppression in the summary and comparison views.
        for panel in walk_panels(overview):
            for expression in self.expressions(panel):
                for selector in re.findall(r"cwo_dispatch_coverage_state\{([^}]+)\}", expression):
                    self.assertNotIn("token_kind", selector)

    def test_overview_uses_current_cwo_sources_before_optional_jobs(self):
        sections = [p["title"] for p in self.dashboard["panels"] if p["type"] == "row" and not p["collapsed"]]
        self.assertEqual(sections, ["CWO activity · whole Codex profile",
                                   "Helper commands and workflow activity"])
        root = self.dashboard["panels"]
        visible = [p for p in root if p["type"] != "row"]
        # A healthy legacy ledger with no recent jobs cannot blank the main view.
        for panel in visible:
            self.assertNotIn("cwo_dispatch_", " ".join(self.expressions(panel)))
        headlines = [p for p in visible if p["gridPos"]["y"] == 3]
        self.assertEqual({p["id"] for p in headlines}, {301, 302, 303, 304})
        for panel in headlines:
            self.assertTrue(any("cwo_codex_" in q for q in self.expressions(panel)))
        table = next(p for p in root if p["id"] == 305)
        self.assertLessEqual(table["gridPos"]["y"], 6)
        self.assertIn("whole-session", next(p for p in root if p["id"] == 303)["description"].lower())
        optional = next(p for p in root if p["id"] == 150)
        self.assertTrue(optional["collapsed"])
        self.assertEqual({p["id"] for p in optional["panels"]}, {3, 101, 102, 103, 104, 110, 111, 112, 121})
        variables = {v["name"]: v for v in self.dashboard["templating"]["list"]}
        self.assertEqual(variables["project"]["label"], "Observed project")
        self.assertEqual(variables["dispatch"]["label"], "Observed task")
        self.assertFalse(any(p["type"] in ("text", "piechart", "timeseries") for p in walk_panels(self.dashboard["panels"])))
        for title in ("Completed tasks", "Stopped or failed"):
            query = self.panel(title)["targets"][0]["expr"]
            self.assertIn("cwo_dispatch_state", query)
            self.assertIn("max by (project_id, dispatch_id)", query)


    def test_tokens_and_responses_keep_provenance_after_deduplication(self):
        for title in ("Tokens reported", "Where tokens went"):
            expression = self.panel(title)["targets"][0]["expr"]
            self.assertIn('token_kind="total"', expression)
            self.assertIn("cwo_dispatch_token_state", expression)
            self.assertIn("unless on (project_id, dispatch_id)", expression)
            self.assertIn("cwo_dispatch_coverage_state", expression)
            self.assertNotIn("vector(0)", expression)
        responses = next(t["expr"] for t in self.panel("Work and results")["targets"] if t["refId"] == "D")
        self.assertIn("cwo_dispatch_completed_cycles_total", responses)
        self.assertIn("cwo_dispatch_coverage_state", responses)


    def test_scrape_example_matches_quick_start_and_separates_optional_dispatches(self) -> None:
        guide = (ROOT / "references/getting-started.mdx").read_text()
        expected = re.search(r"<!-- prometheus-scrape -->(?:\s*\*/})?\s*```yaml\n(.*?)\n```", guide, re.S)[1]
        active = "\n".join(line for line in self.scrape.splitlines()
                           if line.strip() and not line.lstrip().startswith("#"))
        self.assertEqual(active, expected)
        self.assertIn("job_name: traceonaut\n", active)
        self.assertIn("scrape_interval: 5s", active)
        self.assertIn("scrape_timeout: 4s", active)
        self.assertIn("type: Bearer", active)
        self.assertIn("credentials_file: /absolute/path/to/traceonaut/metrics.token", active)
        self.assertIn("targets: ['127.0.0.1:9464']", active)
        self.assertNotIn("credentials:", self.scrape)
        self.assertIn("  # - job_name: traceonaut-dispatches", self.scrape)
        self.assertIn("  #     - targets: ['127.0.0.1:9465']", self.scrape)
        self.assertIn("--state-dir", self.scrape)


if __name__ == "__main__":
    unittest.main()
