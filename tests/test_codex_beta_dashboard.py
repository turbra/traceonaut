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
    def setUpClass(cls) -> None:
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        cls.panels = walk_panels(cls.dashboard["panels"])

    def panel(self, title: str) -> dict:
        matches = [panel for panel in self.panels if panel.get("title") == title]
        self.assertEqual(len(matches), 1, title)
        return matches[0]

    def panel_id(self, identity: int) -> dict:
        return next(panel for panel in self.panels if panel.get("id") == identity)

    def test_cwo_dispatches_use_a_separate_dashboard(self) -> None:
        self.assertNotIn("CWO observed dispatches", {p.get("title") for p in self.panels})
        serialized = json.dumps(self.dashboard)
        self.assertNotIn("cwo_dispatch_", serialized)
        self.assertNotIn("cwo_telemetry_", serialized)

    def test_is_a_distinct_portable_native_grafana_11_5_dashboard(self) -> None:
        self.assertEqual(self.dashboard["uid"], "cwo-codex-beta")
        self.assertEqual(self.dashboard["title"], "Work Overview")
        self.assertEqual(self.dashboard["schemaVersion"], 39)
        self.assertEqual(self.dashboard["time"], {"from": "now-30m", "to": "now"})
        self.assertEqual(self.dashboard["refresh"], "5s")
        self.assertEqual(self.dashboard["__inputs"][0]["name"], "DS_PROMETHEUS")
        self.assertNotIn("scenes", self.dashboard)
        self.assertLessEqual(
            {panel["type"] for panel in self.panels},
            {"bargauge", "piechart", "row", "stat", "table", "text", "timeseries"},
        )
        for panel in self.panels:
            if panel["type"] in {"row", "text"}:
                continue
            self.assertEqual(panel["datasource"]["uid"], "${DS_PROMETHEUS}")
            for target in panel.get("targets", []):
                self.assertEqual(target["datasource"]["uid"], "${DS_PROMETHEUS}")

    def test_visible_project_and_friendly_work_scope_contract(self) -> None:
        variables = {
            variable["name"]: variable
            for variable in self.dashboard["templating"]["list"]
        }
        self.assertEqual(list(variables), ["project", "session"])
        for name, label in (("project", "Project"), ("session", "Work")):
            variable = variables[name]
            self.assertEqual(variable["label"], label)
            self.assertEqual(variable["type"], "custom")
            self.assertEqual(variable["hide"], 0)
            self.assertTrue(variable["multi"])
            self.assertTrue(variable["includeAll"])
            self.assertEqual(variable["allValue"], ".+")
            self.assertNotIn("datasource", variable)
            self.assertNotIn("definition", variable)

        header = sorted((p["gridPos"] for p in self.dashboard["panels"]
                         if p["gridPos"]["y"] == 0), key=lambda pos: pos["x"])
        end = 0
        for position in header:
            self.assertEqual(position["x"], end, "header panels must not overlap or leave gaps")
            end += position["w"]
        self.assertEqual(end, 24)

        scope = self.panel_id(5)
        content = scope["options"]["content"]
        self.assertIn("Work: ${session:text}", content)
        clear = re.search(r'<a href="([^"]+)">All work</a>', content).group(1)
        compare = re.search(r'<a href="([^"]+)">All Sessions</a>', content).group(1)
        self.assertIn("${project:queryparam}", clear)
        self.assertIn("var-session=$__all", clear)
        self.assertIn("${__url_time_range}", clear)
        self.assertIn("${project:queryparam}", compare)
        self.assertIn("${session:queryparam}", compare)
        self.assertIn("${__url_time_range}", compare)
        self.assertIn("independent filters", scope["description"])
        self.assertIn("No work matches", scope["description"])

    def test_visual_hierarchy_keeps_activity_and_one_work_table_before_history(self) -> None:
        summary = [
            self.panel(title)
            for title in ("Working", "Waiting", "Commands recorded", "Recorded failures")
        ]
        activity = self.panel("Working sessions over time")
        work = self.panel_id(11)
        history = self.panel("Observed session history")
        self.assertTrue(all(panel["gridPos"]["y"] < activity["gridPos"]["y"] for panel in summary))
        self.assertLess(activity["gridPos"]["y"], work["gridPos"]["y"])
        self.assertLess(work["gridPos"]["y"], history["gridPos"]["y"])
        self.assertEqual(
            [panel for panel in self.dashboard["panels"] if panel["type"] == "table" and panel["id"] != 103],
            [work],
        )
        diagnostics = self.panel("Diagnostics and detailed coverage")
        self.assertTrue(diagnostics["collapsed"])
        self.assertGreater(diagnostics["gridPos"]["y"], history["gridPos"]["y"])

    def test_health_row_is_global_explicit_and_not_alerting(self) -> None:
        for identity in (101, 102, 103):
            panel = self.panel_id(identity)
            self.assertEqual(panel["gridPos"]["y"], 8)
            self.assertEqual(panel["gridPos"]["h"], 4)
            self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "Unavailable")
            self.assertNotIn("alert", panel)
            for target in panel["targets"]:
                self.assertTrue(target["instant"])
                self.assertFalse(target["range"])
                self.assertNotIn("$project", target["expr"])
                self.assertNotIn("$session", target["expr"])
                self.assertNotIn("vector(0)", target["expr"])
        self.assertEqual(self.panel_id(101)["targets"][0]["expr"],
                         "time() - (max(cwo_codex_collector_scan_timestamp_seconds) > 0)")
        self.assertEqual(self.panel_id(102)["targets"][0]["expr"],
                         "sum(rate(cwo_codex_collector_errors_total[1h]))")
        self.assertEqual(self.panel_id(103)["targets"][0]["expr"],
                         "sum by (reason) (cwo_codex_collector_skipped_records_total)")
        self.assertIn("past end time", self.panel_id(31)["options"]["content"])
        self.assertIn("still exported", self.panel_id(31)["options"]["content"])
        self.assertEqual(len(self.panel_id(104)["targets"]), 5)
        retention = self.panel_id(104)
        self.assertEqual(retention["fieldConfig"]["defaults"]["unit"], "none")
        self.assertNotIn("decimals", retention["fieldConfig"]["defaults"])
        self.assertTrue(retention["targets"][0]["expr"].endswith(" / 86400"))
        self.assertEqual(retention["targets"][0]["legendFormat"], "Inactivity window (days)")
        # Every visible panel stays inside the 24-column grid without overlap.
        panels = self.dashboard["panels"]
        for index, left in enumerate(panels):
            a = left["gridPos"]
            self.assertLessEqual(a["x"] + a["w"], 24)
            for right in panels[index + 1:]:
                b = right["gridPos"]
                self.assertFalse(a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                                 and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"],
                                 (left["id"], right["id"]))

    def test_activity_is_stepped_and_preserves_collection_gaps(self) -> None:
        activity = self.panel("Working sessions over time")
        target = activity["targets"][0]
        custom = activity["fieldConfig"]["defaults"]["custom"]
        self.assertEqual(custom["axisLabel"], "Working sessions")
        self.assertEqual(custom["lineInterpolation"], "stepAfter")
        self.assertFalse(custom["spanNulls"])
        self.assertTrue(target["range"])
        self.assertFalse(target["instant"])
        self.assertIn("cwo_codex_collector_source_available", target["expr"])
        self.assertIn("cwo_codex_collector_scan_timestamp_seconds", target["expr"])
        self.assertIn("remain gaps", activity["description"])
        self.assertIn("not provider compute time", activity["description"])

    def test_state_ring_shares_activity_row_and_preserves_work_table_capacity(self) -> None:
        activity, ring, work = (self.panel_id(identity) for identity in (21, 23, 11))
        self.assertEqual(activity["gridPos"], {"x": 0, "y": 15, "w": 16, "h": 8})
        self.assertEqual(ring["gridPos"], {"x": 16, "y": 15, "w": 8, "h": 8})
        self.assertEqual(work["gridPos"]["y"], ring["gridPos"]["y"] + ring["gridPos"]["h"])
        self.assertEqual(work["gridPos"]["h"], 11)
        self.assertEqual(ring["type"], "piechart")
        self.assertEqual(ring["options"]["pieType"], "donut")
        self.assertEqual(ring["options"]["legend"]["values"], ["value"])
        self.assertIn("at range end", ring["title"])
        self.assertEqual(ring["fieldConfig"]["defaults"]["noValue"], "State data unavailable.")

    def test_ring_colors_match_state_values_and_queries_require_complete_population(self) -> None:
        ring = self.panel_id(23)
        table_states = property_value(override(self.panel_id(11), "State"), "mappings")[0]["options"]
        state_targets = [target for target in ring["targets"] if target["refId"] in "ABCDE"]
        self.assertEqual(len(state_targets), len(table_states))
        for state in table_states.values():
            self.assertEqual(property_value(override(ring, state["text"]), "color"),
                             {"mode": "fixed", "fixedColor": state["color"]})
        self.assertEqual({t["legendFormat"] for t in state_targets},
                         {s["text"] for s in table_states.values()})
        for target in state_targets:
            self.assertTrue(target["instant"])
            self.assertFalse(target["range"])
            self.assertTrue(target["expr"].endswith("> 0"))
            self.assertIn("min by (project_id, session_id)", target["expr"])
            self.assertIn("cwo_codex_session_info", target["expr"])
            self.assertIn("cwo_codex_collector_source_available", target["expr"])
            self.assertIn("cwo_codex_collector_scan_timestamp_seconds", target["expr"])

    def test_ring_status_messages_are_not_state_categories(self) -> None:
        ring = self.panel_id(23)
        statuses = {target["refId"]: target for target in ring["targets"] if target["refId"] in "FG"}
        self.assertEqual({ref: target["legendFormat"] for ref, target in statuses.items()},
                         {"F": "No sessions match this selection.", "G": "State data unavailable."})
        selected_count = self.panel_id(3)["targets"][0]["expr"]
        for ref, target in statuses.items():
            self.assertIn(selected_count, target["expr"])
            self.assertTrue(target["instant"])
            self.assertFalse(target["range"])
            mapping = property_value(override(ring, target["legendFormat"]), "mappings")
            self.assertEqual({entry["options"]["match"] for entry in mapping}, {"nan", "null"})
            self.assertEqual({entry["options"]["result"]["text"] for entry in mapping},
                             {"0" if ref == "F" else "-"})
        self.assertIn("min by (project_id, session_id)", statuses["G"]["expr"])

    def test_account_allowance_is_explicitly_account_wide_at_range_end(self) -> None:
        self.assertIn("at range end", self.panel_id(90)["title"])
        for identity in (91, 92, 93, 94):
            panel = self.panel_id(identity)
            self.assertNotIn("timeFrom", panel)
            self.assertNotIn("timeShift", panel)
            self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "Unavailable")
            for target in panel["targets"]:
                self.assertTrue(target["instant"])
                self.assertFalse(target["range"])
                self.assertNotIn("$project", target["expr"])
                self.assertNotIn("$session", target["expr"])
                self.assertIn("count(cwo_codex_account_available) == 1", target["expr"])
        for identity in (91, 92, 93):
            for target in self.panel_id(identity)["targets"]:
                self.assertIn("max(cwo_codex_account_available) == 1", target["expr"])
                self.assertIn("< 150", target["expr"])
                self.assertIn(">= 0", target["expr"])
                self.assertNotIn("or vector(0)", target["expr"])
        for identity in (92, 93):
            for target in self.panel_id(identity)["targets"]:
                self.assertIn("window_minutes == 10080", target["expr"])
                self.assertIn("on (job, instance, window)", target["expr"])

    def test_work_overview_keeps_seven_visible_columns_and_exact_time_detail(self) -> None:
        table = self.panel_id(11)
        self.assertEqual(table["type"], "table")
        self.assertEqual([target["refId"] for target in table["targets"]], list("ABCDEFG"))
        self.assertFalse(
            any(
                re.search(r"\b(?:topk|bottomk|sort_desc|sort)\s*\(", target["expr"])
                for target in table["targets"]
            )
        )
        included = table["transformations"][1]["options"]["include"]["names"]
        renamed = table["transformations"][2]["options"]["renameByName"]
        hidden = {"session_id", "Observed at"}
        visible = [renamed.get(name, name) for name in included if renamed.get(name, name) not in hidden]
        self.assertEqual(
            visible,
            [
                "Work",
                "Model / effort",
                "State",
                "Last observed",
                "Failures",
                "Longest command",
                "Commands",
            ],
        )
        self.assertFalse(table["options"]["footer"]["enablePagination"])
        self.assertIn("at range end", table["description"])
        self.assertIn("during the selected interval", table["description"])
        self.assertIn("not proof of serving identity", table["description"])
        self.assertTrue(property_value(override(table, "Observed at"), "custom.hidden"))
        self.assertEqual(property_value(override(table, "Last observed"), "unit"), "s")
        self.assertIn("ages at range end", table["title"])
        self.assertIn("not age relative to now", table["description"])
        age_link = property_value(override(table, "Last observed"), "links")[0]["url"]
        self.assertIn("viewPanel=41", age_link)
        self.assertIn("${__data.fields.session_id}", age_link)
        self.assertIn("${project:queryparam}", age_link)
        detail = self.panel_id(41)
        self.assertEqual(property_value(override(detail, "Last observed at"), "unit"), "dateTimeAsLocal")
        self.assertEqual(detail["transformations"][2]["options"]["indexByName"]["Value #C"], 2)

    def test_work_title_is_the_drilldown_and_opaque_identity_is_hidden(self) -> None:
        table = self.panel_id(11)
        self.assertTrue(property_value(override(table, "session_id"), "custom.hidden"))
        work_links = property_value(override(table, "Work"), "links")
        self.assertEqual(len(work_links), 1)
        self.assertEqual(work_links[0]["title"], "Focus this work")
        self.assertIn("${__data.fields.session_id}", work_links[0]["url"])
        self.assertIn("${project:queryparam}", work_links[0]["url"])
        self.assertIn("${__url_time_range}", work_links[0]["url"])
        self.assertEqual(property_value(override(table, "State"), "links"), [])
        self.assertIn("Work name unavailable", json.dumps(property_value(override(table, "Work"), "mappings")))

    def test_work_table_preserves_missing_values_and_command_coverage(self) -> None:
        table = self.panel_id(11)
        self.assertEqual(table["fieldConfig"]["defaults"]["noValue"], "Unavailable")
        by_ref = {target["refId"]: target["expr"] for target in table["targets"]}
        for ref_id in "ABC":
            expression = by_ref[ref_id]
            self.assertIn("cwo_codex_collector_source_available", expression)
            self.assertIn("cwo_codex_collector_scan_timestamp_seconds", expression)
        for ref_id in "DEF":
            expression = by_ref[ref_id]
            self.assertIn("cwo_codex_command_event_timestamp_seconds", expression)
            self.assertIn(">= $__from / 1000", expression)
            self.assertIn("<= $__to / 1000", expression)
            self.assertIn("cwo_codex_command_telemetry_ready) == 1", expression)
            self.assertIn(
                "cwo_codex_command_telemetry_complete_after_timestamp_seconds) < $__from",
                expression,
            )
            self.assertIn("cwo_codex_command_telemetry_snapshot_timestamp_seconds", expression)
            self.assertIn("* 0 / 0", expression)
            self.assertIn("cwo_codex_collector_source_available", expression)
            self.assertIn("cwo_codex_collector_scan_timestamp_seconds", expression)
        self.assertNotIn("0 *", by_ref["E"])
        self.assertIn("0 *", by_ref["D"])
        self.assertIn("0 *", by_ref["F"])
        for field in ("Failures", "Longest command", "Commands"):
            field_override = override(table, field)
            self.assertEqual(
                property_value(field_override, "mappings"),
                [
                    {
                        "type": "special",
                        "options": {
                            "match": "nan",
                            "result": {"text": "Unavailable", "color": "gray"},
                        },
                    }
                ],
            )
        self.assertEqual(property_value(override(table, "Longest command"), "unit"), "s")
        self.assertIn("never zero-filled outside coverage", table["description"])

    def test_no_match_zero_and_unavailable_collection_are_distinct(self) -> None:
        # Native stat legends disappear with empty frames; panel titles must
        # still identify each compact status when the source is unavailable.
        for identity, title in ((3, "Work count"), (2, "Collection"), (58, "Command range")):
            self.assertEqual(self.panel_id(identity)["title"], title)
        matches = self.panel_id(3)
        mapping = matches["fieldConfig"]["defaults"]["mappings"][0]["options"]
        self.assertEqual(mapping["0"]["text"], "No work matches")
        self.assertEqual(matches["fieldConfig"]["defaults"]["noValue"], "Unavailable")
        collection = self.panel_id(2)
        statuses = {
            value["text"]
            for value in collection["fieldConfig"]["defaults"]["mappings"][0][
                "options"
            ].values()
        }
        self.assertEqual(statuses, {"Unavailable", "Stale", "Current"})
        coverage = self.panel_id(58)
        coverage_states = coverage["fieldConfig"]["defaults"]["mappings"][0][
            "options"
        ]
        self.assertEqual(coverage_states["0"]["text"], "Incomplete")
        self.assertEqual(coverage_states["1"]["text"], "Covered")
        self.assertEqual(coverage["fieldConfig"]["defaults"]["noValue"], "Unavailable")
        self.assertIn("not a completeness claim", coverage["description"])

    def test_zero_working_is_neutral_and_waiting_is_not_health(self) -> None:
        working = self.panel("Working")
        self.assertEqual(
            working["fieldConfig"]["defaults"]["color"],
            {"mode": "fixed", "fixedColor": "blue"},
        )
        self.assertEqual(working["fieldConfig"]["defaults"]["mappings"][0]["options"]["0"],
                         {"text": "0", "color": "#D8D9DA"})
        self.assertIn({"type": "special", "options": {"match": "null+nan",
            "result": {"text": "Unavailable", "color": "gray"}}},
            working["fieldConfig"]["defaults"]["mappings"])
        self.assertNotIn("healthy", working["description"].lower())
        waiting = self.panel("Waiting")
        self.assertIn("does not establish", waiting["description"])
        self.assertIn("blocked", waiting["description"])
        self.assertIn("unhealthy", waiting["description"])

    def test_recorded_failures_keep_expected_nonzero_qualification(self) -> None:
        failures = self.panel("Recorded failures")
        self.assertIn("expected nonzero exit", failures["description"])
        self.assertIn("not a silent semantic failure detector", failures["description"])
        self.assertIn("expected nonzero exits", failures["targets"][0]["legendFormat"].lower())
        self.assertEqual(failures["fieldConfig"]["defaults"]["color"],
                         {"mode": "fixed", "fixedColor": "#D8D9DA"})
        self.assertNotIn("thresholds", failures["fieldConfig"]["defaults"])
        self.assertNotIn("error rate", json.dumps(self.dashboard).lower())

    def test_history_heading_subtitle_and_totals_do_not_claim_interval_usage(self) -> None:
        history = self.panel("Observed session history")
        self.assertFalse(history["collapsed"])
        subtitle = self.panel_id(31)["options"]["content"]
        self.assertIn("before this interval", subtitle)
        self.assertIn("older records may be missing", subtitle)
        self.assertIn("not interval usage", subtitle.lower())
        for title, token_kind in (
            ("Total tokens", "total"),
            ("Input tokens", "input"),
            ("Output tokens", "output"),
        ):
            panel = self.panel(title)
            defaults = panel["fieldConfig"]["defaults"]
            self.assertEqual(defaults["unit"], "locale")
            self.assertEqual(defaults["noValue"], "Not recorded")
            expression = panel["targets"][0]["expr"]
            self.assertIn(f'token_kind="{token_kind}"', expression)
            self.assertNotIn("increase(", expression)
        self.assertIn("already included", self.panel("Input tokens")["description"])
        self.assertIn("already included", self.panel("Output tokens")["description"])

    def test_history_subset_bars_use_explicit_denominators_and_zero_to_100(self) -> None:
        for title, subset, denominator in (
            ("Cached input / total input", "cached_input", "input"),
            ("Reasoning output / total output", "reasoning_output", "output"),
        ):
            panel = self.panel(title)
            defaults = panel["fieldConfig"]["defaults"]
            self.assertEqual(panel["type"], "bargauge")
            self.assertEqual(defaults["unit"], "percent")
            self.assertEqual(defaults["min"], 0)
            self.assertEqual(defaults["max"], 100)
            self.assertEqual(defaults["noValue"], "Share unavailable")
            expression = panel["targets"][0]["expr"]
            self.assertIn(f'token_kind="{subset}"', expression)
            self.assertIn(f'token_kind="{denominator}"', expression)
            self.assertIn("100 *", expression)
            self.assertIn(" / ", expression)
            self.assertIn("non-additive subset", panel["description"])
            self.assertIn(" divided by ", panel["description"])

    def test_ranked_history_uses_one_zero_based_scale_and_explicit_units(self) -> None:
        for title, unit, field in (
            ("Recorded tokens by work", "locale", "Recorded tokens"),
            ("Observed turn time by work", "s", "Observed turn time"),
        ):
            panel = self.panel(title)
            self.assertEqual(panel["type"], "bargauge")
            self.assertEqual(panel["options"]["orientation"], "horizontal")
            self.assertEqual(panel["options"]["valueMode"], "text")
            defaults = panel["fieldConfig"]["defaults"]
            self.assertEqual(defaults["unit"], unit)
            self.assertEqual(defaults["min"], 0)
            self.assertFalse(defaults["fieldMinMax"])
            self.assertEqual(defaults["noValue"], "Not recorded")
            transformations = {item["id"]: item["options"] for item in panel["transformations"]}
            self.assertEqual(transformations["sortBy"]["sort"], [{"field": field, "desc": True}])
            self.assertIn({"fieldName": "Work", "handlerKey": "field.name"},
                          transformations["rowsToFields"]["mappings"])
            self.assertIn("before the selected interval", panel["description"])
            self.assertIn("Missing values stay missing", panel["description"])
        self.assertIn("not elapsed session duration", self.panel_id(81)["description"])

    def test_state_colors_are_bound_to_values_and_waiting_is_neutral(self) -> None:
        expected = {
            "0": {"text": "Unknown", "color": "gray"},
            "1": {"text": "Working", "color": "blue"},
            "2": {"text": "Waiting", "color": "#D8D9DA"},
            "3": {"text": "Stopped / failed", "color": "orange"},
            "4": {"text": "No recent signal", "color": "yellow"},
        }
        for identity in (11, 41):
            self.assertEqual(property_value(override(self.panel_id(identity), "State"), "mappings"),
                             [{"type": "value", "options": expected}])
        self.assertEqual(self.panel("Waiting")["fieldConfig"]["defaults"]["color"]["fixedColor"],
                         expected["2"]["color"])

    def test_usage_coverage_keeps_missing_partial_and_conflicted_distinct(self) -> None:
        summary = self.panel("History coverage")
        self.assertEqual(summary["fieldConfig"]["defaults"]["noValue"], "Unavailable")
        self.assertIn("Partial / missing", summary["description"])
        coverage = self.panel("Selected-record coverage")
        self.assertEqual(
            [target["legendFormat"] for target in coverage["targets"]],
            ["Complete", "Partial", "Runtime only", "Unknown", "Conflicted"],
        )
        for target in coverage["targets"]:
            self.assertIn('project_id=~"$project",session_id=~"$session"', target["expr"])
            self.assertIn(">= $__from / 1000", target["expr"])
            self.assertIn("<= $__to / 1000", target["expr"])
            self.assertIn("cwo_codex_collector_source_available", target["expr"])

        detail = self.panel("Recorded usage by work")
        self.assertEqual(detail["transformations"][0]["options"]["mode"], "outer")
        self.assertEqual(detail["fieldConfig"]["defaults"]["noValue"], "Not recorded")
        self.assertIn("a measured zero remains 0", detail["description"])
        self.assertIn("not active compute", detail["description"])
        self.assertIn("or ETA", detail["description"])

    def test_unknown_outcome_and_compaction_diagnostics_remain_available(self) -> None:
        unknown = self.panel("Unknown outcomes")
        self.assertIn('outcome=~"unknown|conflict"', unknown["targets"][0]["expr"])
        self.assertIn("lower bound", unknown["description"])
        self.assertIn("never counted as successful", unknown["description"])
        unknown_table = self.panel("Work with unknown outcomes")
        self.assertTrue(property_value(override(unknown_table, "session_id"), "custom.hidden"))
        self.assertIn('outcome=~\\"unknown|conflict\\"', json.dumps(unknown_table["targets"]))

        compactions = self.panel("Observed compactions")
        expression = compactions["targets"][0]["expr"]
        self.assertIn("cwo_codex_compaction_observation_timestamp_seconds", expression)
        self.assertIn(">= $__from / 1000", expression)
        self.assertIn("<= $__to / 1000", expression)
        self.assertIn("Trigger and effect are unavailable", compactions["description"])
        self.assertIn("not evidence of sufficient context headroom", compactions["description"])
        context = self.panel("What context activity tells you")["options"]["content"]
        self.assertIn("Context utilization", context)
        self.assertIn("remaining space", context)
        self.assertIn("overflow are not measured", context)

    def test_queries_use_only_declared_sources_and_keep_private_data_out(self) -> None:
        pattern = re.compile(r"\b(cwo_codex_[a-z0-9_]+)\b")
        observed: set[str] = set()
        for panel in self.panels:
            for target in panel.get("targets", []):
                expression = target["expr"]
                metrics = set(pattern.findall(expression))
                self.assertTrue(metrics, expression)
                self.assertLessEqual(metrics, ALLOWED_METRICS, expression)
                self.assertNotIn("last_over_time(", expression)
                self.assertNotRegex(
                    expression,
                    r'(?:title|project_name|agent_name|prompt)\s*=|/home/|127\.0\.0\.1',
                )
                observed.update(metrics)
        self.assertEqual(observed, ALLOWED_METRICS)

        serialized = json.dumps(self.dashboard)
        for private_value in (
            "/home/",
            "127.0.0.1",
            "localhost",
            "password",
            "712.7",
        ):
            self.assertNotIn(private_value, serialized.lower())
        self.assertNotRegex(
            serialized,
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        )

    def test_all_data_links_preserve_dashboard_range(self) -> None:
        links: list[str] = []
        for panel in self.panels:
            for field_override in panel.get("fieldConfig", {}).get("overrides", []):
                for field_property in field_override.get("properties", []):
                    if field_property.get("id") == "links":
                        links.extend(item["url"] for item in field_property["value"])
        links.extend(
            re.findall(r"\]\((/d/[^)]+)\)", self.panel_id(5)["options"]["content"])
        )
        self.assertGreaterEqual(len(links), 4)
        for link in links:
            self.assertIn("${__url_time_range}", link)
            self.assertNotIn("from=${__from}", link)
            self.assertNotIn("to=${__to}", link)
            if link.startswith("/d/cwo-codex-beta/") or link.startswith("/d/cwo-codex-beta?"):
                self.assertTrue(link.startswith(
                    "/d/cwo-codex-beta/work-overview?"
                ), "Self-navigation must not redirect and cancel the refreshed queries")

    def test_field_colors_use_modes_supported_by_grafana_11_5(self) -> None:
        def visit(value):
            if isinstance(value, dict):
                color = value.get("color")
                if isinstance(color, dict) and "mode" in color:
                    self.assertIn(color["mode"], {"fixed", "thresholds"})
                if value.get("id") == "color" and isinstance(value.get("value"), dict):
                    self.assertIn(value["value"].get("mode"), {"fixed", "thresholds"})
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(self.dashboard)


if __name__ == "__main__":
    unittest.main()
