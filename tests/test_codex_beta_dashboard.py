from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = (
    ROOT / "examples" / "observability" / "codex-work-overview-beta.json"
)

ALLOWED_METRICS = {
    "cwo_codex_account_available",
    "cwo_codex_account_last_success_timestamp_seconds",
    "cwo_codex_account_reset_credits_available",
    "cwo_codex_account_window_minutes",
    "cwo_codex_account_window_used_percent",
    "cwo_codex_account_window_reset_timestamp_seconds",
    "cwo_codex_collector_errors_total",
    "cwo_codex_collector_skipped_records_total",
    "cwo_codex_collector_session_export_retention_seconds",
    "cwo_codex_collector_session_export_cap",
    "cwo_codex_collector_session_export_exported_sessions",
    "cwo_codex_collector_session_export_expired_sessions",
    "cwo_codex_collector_session_export_cap_omitted_sessions",
    "cwo_codex_collector_last_event_timestamp_seconds",
    "cwo_codex_collector_pending_files",
    "cwo_codex_collector_scan_timestamp_seconds",
    "cwo_codex_collector_sessions",
    "cwo_codex_collector_source_available",
    "cwo_codex_command_event_duration_seconds",
    "cwo_codex_command_event_timestamp_seconds",
    "cwo_codex_command_telemetry_complete_after_timestamp_seconds",
    "cwo_codex_command_telemetry_ready",
    "cwo_codex_command_telemetry_snapshot_timestamp_seconds",
    "cwo_codex_compaction_observation_timestamp_seconds",
    "cwo_codex_compaction_telemetry_complete_after_timestamp_seconds",
    "cwo_codex_compaction_telemetry_ready",
    "cwo_codex_compaction_telemetry_snapshot_timestamp_seconds",
    "cwo_codex_session_completed_turns",
    "cwo_codex_session_failed_turns",
    "cwo_codex_session_info",
    "cwo_codex_session_last_event_timestamp_seconds",
    "cwo_codex_session_observed_turn_seconds",
    "cwo_codex_session_response_count",
    "cwo_codex_session_state",
    "cwo_codex_session_usage_state",
    "cwo_codex_session_usage_tokens",
}


def walk_panels(panels: list[dict]) -> list[dict]:
    result: list[dict] = []
    for panel in panels:
        result.append(panel)
        result.extend(walk_panels(panel.get("panels", [])))
    return result


def override(panel: dict, field: str) -> dict:
    return next(
        item
        for item in panel["fieldConfig"]["overrides"]
        if item["matcher"] == {"id": "byName", "options": field}
    )


def property_value(field_override: dict, property_id: str):
    return next(
        item["value"]
        for item in field_override["properties"]
        if item["id"] == property_id
    )


class CodexBetaDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text())
        cls.panels = {p["id"]: p for p in walk_panels(cls.dashboard["panels"])}

    def test_portable_identity_scope_and_metric_contract(self):
        self.assertEqual(self.dashboard["uid"], "cwo-codex-beta")
        self.assertEqual(self.dashboard["title"], "Work Overview")
        self.assertEqual(self.dashboard["schemaVersion"], 39)
        self.assertEqual(self.dashboard["time"], {"from": "now-30m", "to": "now"})
        self.assertEqual(self.dashboard["refresh"], "30s")
        self.assertEqual(self.dashboard["__inputs"][0]["name"], "DS_PROMETHEUS")
        for variable, name in zip(self.dashboard["templating"]["list"], ("project", "session")):
            self.assertEqual(variable["name"], name)
            self.assertEqual(variable["type"], "custom")
            self.assertTrue(variable["multi"] and variable["includeAll"])
            self.assertEqual(variable["allValue"], ".+")
        observed = set()
        for panel in self.panels.values():
            for target in panel.get("targets", []):
                self.assertEqual(target["datasource"]["uid"], "${DS_PROMETHEUS}")
                metrics = set(re.findall(r"\b(cwo_[a-z0-9_]+)\b", target["expr"]))
                self.assertTrue(metrics)
                observed.update(metrics)
                self.assertNotRegex(target["expr"], r"(?:title|project_name|agent_name|prompt)\s*=")
        self.assertLessEqual(observed, ALLOWED_METRICS)

    def test_primary_content_and_collapsed_details(self):
        self.assertEqual(self.panels[21]["gridPos"], dict(x=0, y=5, w=24, h=7))
        self.assertEqual(self.panels[11]["gridPos"], dict(x=0, y=12, w=24, h=10))
        self.assertTrue(self.panels[30]["collapsed"])
        self.assertTrue(self.panels[40]["collapsed"])
        self.assertIn(42, {p["id"] for p in self.panels[30]["panels"]})
        self.assertIn(103, {p["id"] for p in self.panels[40]["panels"]})
        self.assertFalse({23, 41, 61, 73} & self.panels.keys())
        self.assertEqual(self.panels[7]["title"], "Subagents")

    def test_health_is_global_and_errors_are_per_hour(self):
        for identity in (101, 102, 103):
            for target in self.panels[identity]["targets"]:
                self.assertNotIn("$project", target["expr"])
                self.assertNotIn("$session", target["expr"])
                self.assertNotIn("vector(0)", target["expr"])
        self.assertEqual(self.panels[102]["targets"][0]["expr"],
                         "3600 * (sum(rate(cwo_codex_collector_errors_total[1h])))")
        self.assertEqual(self.panels[103]["targets"][0]["expr"],
                         "sum by (reason) (cwo_codex_collector_skipped_records_total)")
        self.assertEqual(len(self.panels[104]["targets"]), 5)
        self.assertIn("untracked_prefix", self.panels[103]["description"])

    def test_observed_commands_are_separate_from_coverage(self):
        for identity in (51, 52):
            panel = self.panels[identity]
            complete, partial = panel["targets"]
            self.assertIn("and on () ((max(cwo_codex_command_telemetry_ready)", complete["expr"])
            self.assertIn("unless on () ((max(cwo_codex_command_telemetry_ready)", partial["expr"])
            self.assertEqual(partial["legendFormat"], "Partial")
            self.assertEqual(property_value(override(panel, "Partial"), "unit"), "prefix:≥ ")
            for t in panel["targets"]:
                for value in ("max by (project_id, session_id, observation_id)",
                              ">= $__from / 1000", "<= $__to / 1000",
                              "cwo_codex_command_telemetry_snapshot_timestamp_seconds"):
                    self.assertIn(value, t["expr"])
        for target in self.panels[11]["targets"]:
            if target["refId"] in ("D", "E", "F", "I"):
                self.assertNotIn("cwo_codex_command_telemetry_ready", target["expr"])
                self.assertNotIn("cwo_codex_compaction_telemetry_ready", target["expr"])

    def test_work_table_includes_roles_commands_and_independent_compactions(self):
        table = self.panels[11]
        self.assertEqual(table["transformations"][0]["options"], {"byField": "session_id", "mode": "outer"})
        names = set(table["transformations"][2]["options"]["renameByName"].values())
        self.assertTrue({"Work", "Role", "Parent", "State", "Last seen", "Model / effort", "Tokens",
                         "Commands", "Failures", "Longest", "Unknown", "Compactions"} <= names)
        self.assertTrue(property_value(override(table, "session_id"), "custom.hidden"))
        self.assertTrue(property_value(override(table, "Observed at"), "custom.hidden"))
        self.assertIn("kind", table["targets"][0]["expr"])
        compaction = next(t["expr"] for t in table["targets"] if t["refId"] == "J")
        self.assertIn("cwo_codex_compaction_telemetry_ready", compaction)
        self.assertNotIn("cwo_codex_command_telemetry_ready", compaction)
        for field in ("Commands", "Failures", "Unknown", "Tokens"):
            self.assertEqual(property_value(override(table, field), "unit"), "locale")
        self.assertIn("Partial", table["description"])

    def test_history_ratios_and_usage_keep_provenance(self):
        for identity in (34, 36):
            panel = self.panels[identity]
            self.assertEqual(panel["type"], "stat")
            self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "percent")
            self.assertIn(" / ", panel["targets"][0]["expr"])
        for identity in (32, 33, 35, 80):
            self.assertIn("cwo_codex_session_usage_state", self.panels[identity]["targets"][0]["expr"])
        self.assertIn("recorded session history", self.panels[11]["description"])

    def test_activity_is_stepped_and_collection_gaps_remain_gaps(self):
        activity = self.panels[21]
        custom = activity["fieldConfig"]["defaults"]["custom"]
        self.assertEqual(custom["lineInterpolation"], "stepAfter")
        self.assertFalse(custom["spanNulls"])
        target = activity["targets"][0]
        self.assertTrue(target["range"])
        self.assertFalse(target["instant"])
        self.assertIn("cwo_codex_collector_source_available", target["expr"])
        self.assertIn("cwo_codex_collector_scan_timestamp_seconds", target["expr"])

    def test_dashboard_links_replace_banner_and_preserve_session_scope(self):
        for link in self.dashboard["links"]:
            self.assertIn("${__url_time_range}", link["url"])
            if link["title"] in ("Work Overview", "All Sessions"):
                self.assertIn("${project:queryparam}", link["url"])
                self.assertIn("${session:queryparam}", link["url"])


if __name__ == "__main__":
    unittest.main()
