"""Keep historical labels while avoiding duplicate presentation metadata."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_codex_beta_dashboard as beta
import render_codex_sessions_dashboard as stable
import render_codex_unified_dashboard as unified


def snapshot(count=1000):
    return {"version": 1, "session_export": {"cap": 2, "exported_sessions": 2}, "sessions": [
        {"session_id": f"session-{i}", "project_id": "example", "project_name": "Example app",
         "title": f"Historical work {i}", "agent_name": "Reviewer" if i % 2 else "",
         "kind": "subagent" if i % 2 else "session", "parent_id": f"session-{i - 1}" if i % 2 else None}
        for i in range(count)
    ]}


def template(name):
    return json.loads((ROOT / "examples/observability" / name).read_text())


class HistoricalNameTests(unittest.TestCase):
    def test_every_historical_session_remains_in_selectors_tables_and_ranked_bars(self):
        source = snapshot()
        identities = {s["session_id"] for s in source["sessions"]}
        for renderer, file in ((beta, "codex-work-overview-beta.json"), (stable, "codex-all-sessions.json")):
            with self.subTest(dashboard=file):
                result = renderer.render_dashboard(template(file), source)
                variable = next(v for v in result["templating"]["list"] if v["name"] == "session")
                self.assertEqual({v["value"] for v in variable["options"][1:]}, identities)
                identity_entries = 0
                for panel in stable.walk_panels(result["panels"]):
                    overrides = panel.get("fieldConfig", {}).get("overrides", [])
                    if panel["type"] == "bargauge":
                        named = {o["matcher"]["options"] for o in overrides if o["matcher"]["id"] == "byName"}
                        self.assertEqual(named, identities)
                        identity_entries += len(named)
                    for override in overrides:
                        field = override["matcher"].get("options")
                        if field not in ("Work", "Session", "Parent"):
                            continue
                        mappings = next(p["value"] for p in override["properties"] if p["id"] == "mappings")
                        self.assertTrue(identities <= mappings[0]["options"].keys())
                        identity_entries += len(identities & mappings[0]["options"].keys())
                # Two ranked fields and at most three identity columns. This
                # bounds duplication, not which historical sessions are named.
                self.assertLessEqual(identity_entries, 5 * len(identities))

    def test_work_overview_has_one_ranked_link_and_readable_parent_column(self):
        result = beta.render_dashboard(template("codex-work-overview-beta.json"), snapshot(2))
        for panel in stable.walk_panels(result["panels"]):
            if panel["type"] == "bargauge":
                self.assertIn("${__field.name:percentencode}", panel["fieldConfig"]["defaults"]["links"][0]["url"])
                for override in panel["fieldConfig"]["overrides"]:
                    self.assertFalse(any(p["id"] == "links" for p in override["properties"]))
        main = next(p for p in result["panels"] if p["id"] == 11)
        parent = next(o for o in main["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Parent")
        names = next(p["value"] for p in parent["properties"] if p["id"] == "mappings")[0]["options"]
        self.assertEqual(names["session-0"]["text"], "Historical work 0")
        self.assertIn("parent_id", main["targets"][0]["expr"])

    def test_legacy_unified_render_is_unchanged(self):
        result = unified.render_dashboard(template("codex-unified-overview.json"), snapshot(4), "example-prometheus")
        fingerprint = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        self.assertEqual(fingerprint, "748fd5c3d2b8fa67ffb4fac4e7ce4c616011a54cd71f2bccbb054df9c3d0cb0e")
