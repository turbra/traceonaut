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


DASHBOARD_PATH = ROOT / "tests" / "fixtures" / "cwo-controller-queries.json"
SCRAPE_PATH = ROOT / "examples" / "observability" / "prometheus-scrape.yaml"


class ControllerQueryCompatibilityTests(unittest.TestCase):
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
