from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render_codex_sessions_dashboard import (  # noqa: E402
    render_dashboard,
    load_snapshot,
    validate_snapshot,
    walk_panels,
    write_dashboard,
)


DASHBOARD_PATH = ROOT / "examples" / "observability" / "codex-all-sessions.json"
RENDERER_PATH = ROOT / "scripts" / "render_codex_sessions_dashboard.py"
METRICS = {
    "cwo_codex_session_info",
    "cwo_codex_session_last_event_timestamp_seconds",
    "cwo_codex_session_state",
    "cwo_codex_session_usage_tokens",
    "cwo_codex_session_reported_tokens",
    "cwo_codex_session_response_count",
    "cwo_codex_session_completed_turns",
    "cwo_codex_session_failed_turns",
    "cwo_codex_session_observed_turn_seconds",
    "cwo_codex_session_usage_state",
    "cwo_codex_collector_scan_timestamp_seconds",
    "cwo_codex_collector_last_event_timestamp_seconds",
    "cwo_codex_collector_sessions",
    "cwo_codex_collector_pending_files",
    "cwo_codex_collector_source_available",
    "cwo_codex_account_available",
    "cwo_codex_account_last_success_timestamp_seconds",
    "cwo_codex_account_reset_credits_available",
    "cwo_codex_account_window_minutes",
    "cwo_codex_account_window_used_percent",
    "cwo_codex_account_window_reset_timestamp_seconds",
}


def uuid7_at(when: datetime) -> str:
    milliseconds = int(when.timestamp() * 1000)
    value = milliseconds << 80
    value |= 0x7 << 76
    value |= 0x2 << 62
    value |= 1
    return str(UUID(int=value))


class CodexSessionsDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

    def panel(self, title: str) -> dict:
        return next(
            panel
            for panel in walk_panels(self.dashboard["panels"])
            if panel.get("title") == title
        )

    def test_portable_dashboard_replaces_primary_with_thirty_minute_view(self) -> None:
        self.assertEqual(self.dashboard["uid"], "cwo-supervisor-observability-v1")
        self.assertEqual(self.dashboard["title"], "Codex · All sessions")
        self.assertEqual(self.dashboard["time"], {"from": "now-30m", "to": "now"})
        self.assertEqual(self.dashboard["refresh"], "5s")
        self.assertEqual(self.dashboard["schemaVersion"], 39)
        self.assertNotIn("scenes", self.dashboard)
        self.assertEqual(
            [variable["name"] for variable in self.dashboard["templating"]["list"]],
            ["project", "session"],
        )
        self.assertEqual(self.dashboard["__inputs"][0]["name"], "DS_PROMETHEUS")
        for panel in walk_panels(self.dashboard["panels"]):
            if panel["type"] in {"row", "text"}:
                continue
            self.assertEqual(panel["datasource"]["uid"], "${DS_PROMETHEUS}")
            for target in panel.get("targets", []):
                self.assertEqual(target["datasource"]["uid"], "${DS_PROMETHEUS}")

    def test_queries_use_only_the_sessions_and_account_metric_contract(self) -> None:
        pattern = re.compile(r"\b(cwo_codex_[a-z0-9_]+)\b")
        observed = set()
        for panel in walk_panels(self.dashboard["panels"]):
            for target in panel.get("targets", []):
                expression = target["expr"]
                metrics = set(pattern.findall(expression))
                self.assertTrue(metrics, expression)
                self.assertLessEqual(metrics, METRICS, expression)
                self.assertNotIn('\\"', expression)
                self.assertNotIn("last_over_time(", expression)
                self.assertNotRegex(expression, r"(?:title|project_name|agent_name|prompt)\s*=")
                observed.update(metrics)
        self.assertEqual(observed, METRICS)

    def test_range_selects_sessions_by_latest_event(self) -> None:
        table = self.panel("Sessions and latest activity")
        self.assertTrue(all(target["format"] == "table" for target in table["targets"]))
        self.assertEqual(table["transformations"][0]["options"]["mode"], "outer")
        for target in table["targets"]:
            expression = target["expr"]
            self.assertIn("cwo_codex_session_last_event_timestamp_seconds", expression)
            self.assertIn(">= $__from / 1000", expression)
            self.assertIn("<= $__to / 1000", expression)
        shown = table["transformations"][1]["options"]["include"]["names"]
        for raw_identity in ("project_id", "parent_id"):
            self.assertNotIn(raw_identity, shown)
        renamed = set(table["transformations"][2]["options"]["renameByName"].values())
        self.assertTrue(
            {
                "Session",
                "State",
                "Last seen",
                "Project",
                "Agent",
                "Latest selected model",
                "Latest selected effort",
                "Recorded tokens",
                "Runtime reported",
                "Usage source",
            }.issubset(renamed)
        )
        session_override = next(
            item
            for item in table["fieldConfig"]["overrides"]
            if item["matcher"]["options"] == "Session"
        )
        links = next(
            prop["value"]
            for prop in session_override["properties"]
            if prop["id"] == "links"
        )
        self.assertTrue(links[0]["url"].startswith("/d/" + self.dashboard["uid"] + "?"))
        self.assertIn("var-session=${__value.raw}", links[0]["url"])
        self.assertIn("from=${__from}&to=${__to}", links[0]["url"])

    def test_activity_is_observed_only_and_state_wording_is_bounded(self) -> None:
        activity = self.panel("Working sessions over time")
        expression = activity["targets"][0]["expr"]
        self.assertTrue(activity["targets"][0]["range"])
        self.assertFalse(activity["targets"][0]["instant"])
        self.assertLessEqual(activity["maxDataPoints"], 10000)
        self.assertIn("cwo_codex_session_state", expression)
        self.assertIn("== 1", expression)
        self.assertIn("max by (project_id, session_id)", expression)
        self.assertIn("cwo_codex_collector_source_available", expression)
        self.assertIn("cwo_codex_collector_scan_timestamp_seconds", expression)
        self.assertIn("< 20", expression)
        self.assertNotIn("rate(", expression)
        self.assertIn("History begins at collector installation", activity["description"])
        self.assertIn("missing heartbeat periods remain gaps", activity["description"])
        states = self.panel("Current session states")
        self.assertIn("does not claim the session is finished", states["description"])
        self.assertEqual(
            {target["legendFormat"] for target in states["targets"]},
            {"Working", "Waiting", "Stopped / failed", "No recent signal", "Unknown"},
        )
        self.assertTrue(all(target["expr"].endswith("> 0") for target in states["targets"]))

    def test_health_separates_reader_freshness_from_source_event_age(self) -> None:
        health = self.panel("Collection status")
        latest_event = self.panel("Latest source event age")
        health_expression = health["targets"][0]["expr"]
        self.assertIn("cwo_codex_collector_source_available", health_expression)
        self.assertIn("cwo_codex_collector_scan_timestamp_seconds", health_expression)
        self.assertNotIn("collector_last_event", health_expression)
        self.assertIn("cwo_codex_collector_last_event_timestamp_seconds", latest_event["targets"][0]["expr"])
        self.assertIn("valid when Codex is quiet", latest_event["description"])

    def test_observed_history_is_not_presented_as_range_delta_or_full_lifetime(self) -> None:
        for title in (
            "Recorded session tokens",
            "Sessions and latest activity",
            "Input tokens recorded",
            "Output tokens recorded",
            "Observed responses",
            "Recorded tokens by session",
            "Observed turn time by session",
        ):
            description = self.panel(title)["description"].lower()
            self.assertIn("available", description)
        self.assertIn("may omit older usage", self.panel("Recorded session tokens")["description"])
        self.assertIn("not tokens spent within the selected range", self.panel("Recorded session tokens")["description"])
        coverage = self.panel("")["options"]["content"]
        self.assertIn("latest observed event", coverage)
        self.assertIn("available observed session history", coverage)
        self.assertIn("may omit older usage", coverage)
        self.assertIn("not backfilled", coverage)

    def test_usage_conflict_suppresses_recorded_tokens_and_response_count(self) -> None:
        table = self.panel("Sessions and latest activity")
        recorded = next(target["expr"] for target in table["targets"] if target["refId"] == "C")
        responses = next(target["expr"] for target in table["targets"] if target["refId"] == "E")
        for expression in (recorded, responses, self.panel("Observed responses")["targets"][0]["expr"]):
            self.assertIn("cwo_codex_session_usage_state", expression)
            self.assertIn("== 1", expression)
            self.assertIn("== 4", expression)
        usage = next(
            item for item in table["fieldConfig"]["overrides"]
            if item["matcher"]["options"] == "Usage source"
        )
        mapping = next(prop["value"] for prop in usage["properties"] if prop["id"] == "mappings")
        self.assertEqual(mapping[0]["options"]["3"]["text"], "Usage invalid or conflicted")

    def test_model_and_effort_are_labeled_as_latest_selected_settings(self) -> None:
        table = self.panel("Sessions and latest activity")
        names = set(table["transformations"][2]["options"]["renameByName"].values())
        self.assertIn("Latest selected model", names)
        self.assertIn("Latest selected effort", names)
        self.assertNotIn("Model", names)
        self.assertNotIn("Effort", names)
        self.assertIn("not per-model lifetime attribution", table["description"])

    def test_range_and_history_scope_is_prominent_near_summary_cards(self) -> None:
        scope = self.panel("Selected range and recorded history")
        self.assertEqual(scope["type"], "text")
        self.assertEqual(scope["gridPos"], {"x": 0, "y": 12, "w": 24, "h": 4})
        content = scope["options"]["content"]
        self.assertIn("latest observed event", content)
        self.assertIn("available observed session history", content)
        self.assertIn("not activity spent within the selected range", content)
        self.assertIn("may omit older usage or records", content)

    def test_comparison_bars_receive_table_fields_for_human_name_mapping(self) -> None:
        for title in ("Recorded tokens by session", "Observed turn time by session"):
            panel = self.panel(title)
            self.assertTrue(all(target["format"] == "table" for target in panel["targets"]))
            self.assertEqual(
                [transform["id"] for transform in panel["transformations"]],
                ["filterFieldsByName", "organize", "sortBy", "rowsToFields"],
            )


class CodexSessionsRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        self.project = str(uuid4())
        self.session = uuid7_at(datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc))
        self.snapshot = {
            "version": 1,
            "sessions": [
                {
                    "session_id": self.session,
                    "project_id": self.project,
                    "title": "Review collector coverage",
                    "project_name": "Complex work orchestration",
                    "agent_name": "Dashboard reviewer",
                    "kind": "subagent",
                    "parent_id": str(uuid4()),
                }
            ],
        }

    def test_renderer_changes_presentation_without_changing_queries(self) -> None:
        before = copy.deepcopy(self.template)
        rendered = render_dashboard(self.template, self.snapshot, "prometheus-main")
        self.assertEqual(self.template, before)
        original_queries = [
            (panel["id"], target["expr"])
            for panel in walk_panels(before["panels"])
            for target in panel.get("targets", [])
        ]
        rendered_queries = [
            (panel["id"], target["expr"])
            for panel in walk_panels(rendered["panels"])
            for target in panel.get("targets", [])
        ]
        self.assertEqual(rendered_queries, original_queries)
        self.assertNotIn("__inputs", rendered)
        self.assertNotIn("${DS_PROMETHEUS}", json.dumps(rendered))
        variables = {item["name"]: item for item in rendered["templating"]["list"]}
        self.assertEqual(variables["project"]["options"][1]["text"], "Complex work orchestration")
        self.assertEqual(variables["session"]["options"][1]["text"], "Review collector coverage")
        table = next(panel for panel in rendered["panels"] if panel.get("id") == 110)
        overrides = {
            item["matcher"]["options"]: item for item in table["fieldConfig"]["overrides"]
        }
        expected = {
            "Session": "Review collector coverage",
            "Project": "Complex work orchestration",
            "Agent": "Dashboard reviewer",
        }
        keys = {"Session": self.session, "Project": self.project, "Agent": self.session}
        for field, name in expected.items():
            mapping = next(
                prop["value"]
                for prop in overrides[field]["properties"]
                if prop["id"] == "mappings"
            )
            self.assertEqual(mapping[0]["options"][keys[field]]["text"], name)
            self.assertEqual(mapping[-1]["options"]["pattern"], "^.+$")
            self.assertNotRegex(mapping[-1]["options"]["result"]["text"], self.session)
        for panel in rendered["panels"]:
            if panel.get("type") != "bargauge":
                continue
            fallback = panel["fieldConfig"]["overrides"][0]
            self.assertEqual(fallback["matcher"], {"id": "byRegexp", "options": "/.*/"})
            self.assertEqual(
                fallback["properties"],
                [{"id": "displayName", "value": "Session name unavailable"}],
            )
            override = next(
                item
                for item in panel["fieldConfig"]["overrides"]
                if item["matcher"]["options"] == self.session
            )
            self.assertEqual(
                override["properties"],
                [{"id": "displayName", "value": "Review collector coverage"}],
            )

    def test_missing_title_has_project_kind_and_uuid7_date_fallback(self) -> None:
        self.snapshot["sessions"][0]["title"] = ""
        self.snapshot["sessions"][0]["agent_name"] = ""
        rendered = render_dashboard(self.template, self.snapshot)
        variables = {item["name"]: item for item in rendered["templating"]["list"]}
        self.assertEqual(
            variables["session"]["options"][1]["text"],
            "Complex work orchestration · Subagent · 2026-09-16 12:00:00.000 UTC",
        )
        table = next(panel for panel in rendered["panels"] if panel.get("id") == 110)
        agent = next(
            item for item in table["fieldConfig"]["overrides"] if item["matcher"]["options"] == "Agent"
        )
        mapping = next(prop["value"] for prop in agent["properties"] if prop["id"] == "mappings")
        self.assertEqual(mapping[0]["options"][self.session]["text"], "Subagent")

    def test_duplicate_titles_are_disambiguated_by_human_start_time(self) -> None:
        second = copy.deepcopy(self.snapshot["sessions"][0])
        second["session_id"] = uuid7_at(
            datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
        )
        second["parent_id"] = self.session
        self.snapshot["sessions"].append(second)
        rendered = render_dashboard(self.template, self.snapshot)
        variable = next(
            item for item in rendered["templating"]["list"] if item["name"] == "session"
        )
        labels = {item["value"]: item["text"] for item in variable["options"][1:]}
        self.assertEqual(len(set(labels.values())), 2)
        self.assertIn("2026-09-16 12:00:00.000 UTC", labels[self.session])
        self.assertIn("2026-09-16 13:00:00.000 UTC", labels[second["session_id"]])
        self.assertNotIn(self.session, labels[self.session])

    def test_snapshot_text_cannot_expand_grafana_templates(self) -> None:
        self.snapshot["sessions"][0]["title"] = "Session ${__field.name}"
        self.snapshot["sessions"][0]["project_name"] = "Project $session"
        self.snapshot["sessions"][0]["agent_name"] = "Agent [[session]]"
        rendered = render_dashboard(self.template, self.snapshot)
        variables = {item["name"]: item for item in rendered["templating"]["list"]}
        self.assertEqual(
            variables["session"]["options"][1]["text"],
            "Session title contains unsupported template syntax",
        )
        self.assertEqual(
            variables["project"]["options"][1]["text"],
            "Project name contains unsupported template syntax",
        )
        self.assertNotIn(self.session, variables["session"]["options"][1]["text"])

    def test_snapshot_validation_and_output_path_are_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported session snapshot"):
            validate_snapshot({"version": 2, "sessions": []})
        duplicate = copy.deepcopy(self.snapshot["sessions"][0])
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_snapshot({"version": 1, "sessions": [duplicate, duplicate]})
        with self.assertRaisesRegex(ValueError, "invalid datasource UID"):
            render_dashboard(self.template, self.snapshot, "bad/uid")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            output = root / "dashboard.json"
            rendered = render_dashboard(self.template, self.snapshot)
            self.assertTrue(write_dashboard(output, rendered))
            stamp = output.stat().st_mtime_ns
            self.assertFalse(write_dashboard(output, rendered))
            self.assertEqual(output.stat().st_mtime_ns, stamp)
            link = root / "dashboard-link.json"
            link.symlink_to(output)
            with self.assertRaisesRegex(ValueError, "output path invalid"):
                write_dashboard(link, rendered)

    def test_accounting_fields_in_collector_snapshot_are_ignored(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["sessions"][0].update(
            state=2,
            usage={"total": 42},
            reported_tokens=None,
            last_event=1789574400.0,
        )
        normalized = validate_snapshot(snapshot)
        self.assertEqual(set(normalized["sessions"][0]), {
            "session_id", "project_id", "title", "project_name",
            "agent_name", "kind", "parent_id",
        })
        rendered = render_dashboard(self.template, snapshot)
        variables = {item["name"]: item for item in rendered["templating"]["list"]}
        self.assertEqual(
            variables["session"]["options"][1]["text"],
            "Review collector coverage",
        )

    def test_snapshot_loader_requires_owned_mode_0600_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            snapshot_path = root / "sessions.json"
            snapshot_path.write_text(json.dumps(self.snapshot), encoding="utf-8")
            snapshot_path.chmod(0o600)
            self.assertEqual(load_snapshot(snapshot_path), validate_snapshot(self.snapshot))
            snapshot_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owned mode 0600"):
                load_snapshot(snapshot_path)
            snapshot_path.chmod(0o600)
            link = root / "sessions-link.json"
            link.symlink_to(snapshot_path)
            with self.assertRaisesRegex(ValueError, "snapshot unreadable"):
                load_snapshot(link)

    def test_cli_watch_renders_updated_protected_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            snapshot_path = root / "sessions.json"
            output = root / "dashboard.json"

            def write_snapshot(title: str) -> None:
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["sessions"][0]["title"] = title
                temporary = root / "sessions.tmp"
                temporary.write_text(json.dumps(snapshot), encoding="utf-8")
                temporary.chmod(0o600)
                os.replace(temporary, snapshot_path)

            write_snapshot("Initial session title")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(RENDERER_PATH),
                    "--template",
                    str(DASHBOARD_PATH),
                    "--snapshot-file",
                    str(snapshot_path),
                    "--output",
                    str(output),
                    "--watch-seconds",
                    "1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                def wait_for(value: str) -> None:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll())
                        if output.exists() and value in output.read_text(encoding="utf-8"):
                            return
                        time.sleep(0.05)
                    self.fail("dashboard watcher did not publish the expected name")

                wait_for("Initial session title")
                write_snapshot("Updated session title")
                wait_for("Updated session title")
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
