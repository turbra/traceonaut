"""User-facing names and count units stay consistent across all dashboard views."""

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = {
    "codex-work-overview-beta": ("Work Overview", "cwo-codex-beta"),
    "codex-unified-overview": ("Unified", "cwo-codex-unified"),
    "codex-all-sessions": ("All Sessions", "cwo-supervisor-observability-v1"),
    "cwo-observed-dispatches": ("CWO Dispatches", "cwo-dispatch-observability-v1"),
}
TOKEN_PANELS = {
    "codex-work-overview-beta": {32, 33, 35, 80},
    "codex-unified-overview": {32, 33, 35, 80},
    "codex-all-sessions": {104, 160, 161, 111},
    "cwo-observed-dispatches": {104, 160, 161, 111, 303},
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
                legends = [panel for panel in data["panels"] if panel["type"] == "text"
                           and "Token scale:" in panel["options"]["content"]]
                self.assertEqual(len(legends), 1, "Legend must be visible outside collapsed rows")
                for label in ("K = thousand", "Mil = million", "Bil = billion", "Tri = trillion"):
                    self.assertIn(label, legends[0]["options"]["content"])
                self.assertIn("Exact counts", legends[0]["options"]["content"])
                self.assertGreaterEqual(legends[0]["gridPos"]["h"], 2)

    def test_exact_counts_stay_grouped_and_si_prefixes_are_absent(self):
        for name in DASHBOARDS:
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            grouped = 0
            for node in objects(data):
                with self.subTest(dashboard=name, node=node.get("title", node.get("matcher"))):
                    self.assertNotEqual(node.get("unit"), "sishort")
                    if node.get("unit") == "locale":
                        self.assertEqual(node["decimals"], 0)
                        grouped += 1
                    if node.get("type") == "stat" and "token" in node.get("title", "").lower():
                        self.assertNotIn("valueSize", node.get("options", {}).get("text", {}),
                                         "Token counts must fit the available panel width")
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


if __name__ == "__main__":
    unittest.main()
