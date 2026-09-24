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

    def test_count_defaults_and_overrides_use_grouped_whole_numbers(self):
        for name in DASHBOARDS:
            data = json.loads((ROOT / f"examples/observability/{name}.json").read_text())
            grouped = 0
            for node in objects(data):
                with self.subTest(dashboard=name, node=node.get("title", node.get("matcher"))):
                    self.assertNotIn(node.get("unit"), ("short", "sishort"))
                    if node.get("unit") == "locale":
                        self.assertEqual(node["decimals"], 0)
                        grouped += 1
                    if node.get("type") == "stat" and "token" in node.get("title", "").lower():
                        self.assertNotIn("valueSize", node.get("options", {}).get("text", {}),
                                         "Long token counts must fit the available panel width")
                    properties = {item["id"]: item["value"] for item in node.get("properties", [])}
                    self.assertNotIn(properties.get("unit"), ("short", "sishort"))
                    if properties.get("unit") == "locale":
                        self.assertEqual(properties["decimals"], 0)
                        grouped += 1
            self.assertGreater(grouped, 0)


if __name__ == "__main__":
    unittest.main()
