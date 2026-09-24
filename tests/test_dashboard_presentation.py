"""User-facing names and count units stay consistent across all dashboard views."""

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = {
    "codex-work-overview-beta": ("Work Overview", "cwo-codex-beta"),
    "codex-all-sessions": ("All Sessions", "cwo-supervisor-observability-v1"),
    "cwo-overview": ("CWO Overview", "cwo-dispatch-observability-v1"),
}
TOKEN_PANELS = {
    "codex-work-overview-beta": {32, 33, 35, 80},
    "codex-all-sessions": {104, 111},
    "cwo-overview": {104, 111, 303},
}


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


class DashboardPresentationTests(unittest.TestCase):
    def test_session_table_keeps_fixed_widths_within_viewport_budget(self):
        data = json.loads((ROOT / "examples/observability/codex-all-sessions.json").read_text())
        table = next(node for node in objects(data) if node.get("id") == 110)
        widths = {
            override["matcher"]["options"]: next(
                (prop["value"] for prop in override["properties"] if prop["id"] == "custom.width"), 0
            ) for override in table["fieldConfig"]["overrides"]
        }
        names = set(table["transformations"][2]["options"]["renameByName"].values())
        default_width = table["fieldConfig"]["defaults"]["custom"].get("width", 0)
        fixed = [widths.get(name, 0) or default_width for name in names]
        self.assertLessEqual(sum(fixed), 1700)
        self.assertLessEqual(sum(bool(width) for width in fixed), 3,
                             "Most columns should adapt to the available width")
        minimum = table["fieldConfig"]["defaults"]["custom"].get("minWidth", 150)
        self.assertLessEqual(sum(width or minimum for width in fixed), 1700)

    def test_titles_match_guides_and_uids_remain_compatible(self):
        for name, (title, uid) in DASHBOARDS.items():
            with self.subTest(dashboard=name):
                data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
                self.assertEqual((data["title"], data["uid"]), (title, uid))

    def test_token_summaries_use_native_compact_units_with_a_visible_legend(self):
        for name, expected_ids in TOKEN_PANELS.items():
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            panels = [node for node in objects(data) if "gridPos" in node]
            compact_ids = {
                panel["id"] for panel in panels
                if panel.get("fieldConfig", {}).get("defaults", {}).get("unit") == "short"
            }
            with self.subTest(dashboard=name):
                self.assertEqual(compact_ids, expected_ids)
                for panel in panels:
                    if panel["id"] in expected_ids:
                        self.assertIn(panel["type"], ("stat", "bargauge"))
                        self.assertEqual(panel["fieldConfig"]["defaults"]["decimals"], 1)
                self.assertFalse(any(p["type"] == "text" for p in panels))
                for panel in panels:
                    if panel["id"] in expected_ids:
                        for label in ("K = thousand", "Mil = million", "Bil = billion", "Tri = trillion"):
                            self.assertIn(label, panel["description"])
                        self.assertIn("reading-values/", panel["description"])

    def test_exact_counts_stay_grouped_and_si_prefixes_are_absent(self):
        for name in DASHBOARDS:
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            grouped = 0
            for node in objects(data):
                with self.subTest(dashboard=name, node=node.get("title", node.get("matcher"))):
                    self.assertNotEqual(node.get("unit"), "sishort")
                    if node.get("unit") == "locale":
                        self.assertIn(node["decimals"], (0, 1))
                        grouped += 1
                    if node.get("type") == "stat" and "token" in node.get("title", "").lower():
                        self.assertLessEqual(node.get("options", {}).get("text", {}).get("valueSize", 20), 22)
                    properties = {item["id"]: item["value"] for item in node.get("properties", [])}
                    self.assertNotIn(properties.get("unit"), ("short", "sishort"))
                    if properties.get("unit") == "locale":
                        self.assertEqual(properties["decimals"], 0)
                        grouped += 1
            self.assertGreater(grouped, 0)

    def test_expanded_panels_do_not_overlap(self):
        for name in DASHBOARDS:
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            for index, panel in enumerate(data["panels"]):
                a = panel["gridPos"]
                for other in data["panels"][index + 1:]:
                    b = other["gridPos"]
                    with self.subTest(dashboard=name, panels=(panel["id"], other["id"])):
                        self.assertFalse(
                            a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
                        )

    def test_active_dashboards_have_consistent_compact_status_and_no_prose_panels(self):
        for name in DASHBOARDS:
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            top = sorted([p for p in data["panels"] if p["gridPos"]["y"] == 0], key=lambda p: p["gridPos"]["x"])
            self.assertEqual(sum(p["gridPos"]["w"] for p in top), 24)
            cursor = 0
            for p in top:
                self.assertEqual(p["gridPos"]["h"], 2)
                self.assertEqual(p["gridPos"]["x"], cursor)
                cursor += p["gridPos"]["w"]
                self.assertEqual(p["type"], "stat")
            for p in objects(data):
                if "gridPos" not in p or p["type"] == "row":
                    continue
                self.assertNotIn(p["type"], ("text", "piechart"))
                self.assertEqual(p["fieldConfig"]["defaults"]["noValue"], "—")
                if p["type"] == "stat":
                    self.assertEqual(p["options"]["colorMode"], "none")
                if p["type"] == "bargauge":
                    self.assertEqual(p["options"]["namePlacement"], "left")
                    self.assertTrue(p["fieldConfig"]["defaults"].get("displayName"),
                                    "Grafana must retain labels for single-result bar charts")
                    self.assertEqual(p["options"]["minVizHeight"], p["options"]["maxVizHeight"])
                    self.assertTrue(all(t["expr"].startswith("topk(10, ") for t in p["targets"]))
            self.assertEqual(data["refresh"], "1m" if name == "cwo-overview" else "30s")

    def test_session_state_colors_are_identical_everywhere(self):
        states = []
        for name in DASHBOARDS:
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            for p in objects(data):
                if p.get("matcher", {}).get("options") == "State":
                    mapping = next(v["value"] for v in p["properties"] if v["id"] == "mappings")
                    states.append(mapping[0]["options"])
        self.assertGreaterEqual(len(states), 3)
        for state in states:
            self.assertEqual(state, states[0])
            self.assertEqual(state["1"]["color"], "blue")
            self.assertEqual(state["2"]["color"], "#D8D9DA")


if __name__ == "__main__":
    unittest.main()
