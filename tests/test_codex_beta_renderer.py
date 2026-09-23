from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render_codex_beta_dashboard import (  # noqa: E402
    BETA_UID, load_labels, presentation_names, render_dashboard,
    validate_labels, write_beta_dashboard,
)


def snapshot():
    return {"version": 1, "sessions": [
        {"session_id": "parent", "project_id": "source", "title": "Build dashboard",
         "project_name": "CWO", "agent_name": "", "kind": "session", "parent_id": None},
        {"session_id": "child", "project_id": "source", "title": "Review metrics",
         "project_name": "CWO", "agent_name": "Reviewer", "kind": "subagent", "parent_id": "parent"},
        {"session_id": "other", "project_id": "temporary", "title": "Check installation",
         "project_name": "CWO", "agent_name": "", "kind": "session", "parent_id": None},
    ]}


def template():
    return {"uid": BETA_UID, "templating": {"list": [
        {"name": "project", "type": "query", "query": "projects", "multi": True, "includeAll": True},
        {"name": "session", "label": "Work", "type": "custom", "hide": 0,
         "query": "template placeholder", "multi": True, "includeAll": True,
         "allValue": ".+"},
    ]}, "panels": [{"type": "row", "panels": [{
        "id": 11, "type": "table", "targets": [{"expr": 'label_join(cwo_codex_session_info,"model_effort"," · ","model","effort")'}],
        "fieldConfig": {"overrides": [
            {"matcher": {"id": "byName", "options": name},
             "properties": [{"id": "mappings", "value": []}]}
            for name in ("Work", "Session", "Project", "Agent", "Parent")
        ]},
    }]}]}


def grafana_11_5_custom_options(query: str) -> list[tuple[str, str]]:
    """Mirror Grafana 11.5 custom-variable parsing, independently of the renderer."""
    parsed = []
    for raw_option in re.findall(r"(?:\\,|[^,])+", query):
        option = raw_option.strip()
        named = re.fullmatch(r"(.+)\s:\s(.+)", option)
        if named:
            parsed.append((named.group(1).replace(r"\,", ","), named.group(2)))
        else:
            value = option.replace(r"\,", ",")
            parsed.append((value, value))
    return parsed


class CodexBetaRendererTests(unittest.TestCase):
    def test_ranked_bars_have_friendly_labels_and_scoped_work_links(self):
        draft = json.loads((ROOT / "examples/observability/codex-work-overview-beta.json").read_text())
        original = copy.deepcopy(draft)
        source = snapshot()
        source["sessions"][1]["prompt"] = "PRIVATE PROMPT MUST NOT LEAK"
        result = render_dashboard(draft, source, "prometheus", {
            "version": 1, "sessions": {"parent": "Observability dashboard"},
        })
        panels = {panel["id"]: panel for panel in result["panels"]}
        for identity in (80, 81):
            overrides = panels[identity]["fieldConfig"]["overrides"]
            fields = {
                item["matcher"]["options"]: {prop["id"]: prop["value"] for prop in item["properties"]}
                for item in overrides if item["matcher"]["id"] == "byName"
            }
            self.assertEqual(fields["parent"]["displayName"], "Observability dashboard")
            self.assertEqual(fields["child"]["displayName"], "Review metrics")
            for session in ("parent", "child", "other"):
                self.assertEqual(fields[session]["links"], [{
                    "title": "Focus this work",
                    "url": "/d/cwo-codex-beta/codex-c2b7-work-overview-c2b7-beta"
                    + "?${project:queryparam}&var-session="
                    + session + "&${__url_time_range}",
                }])
            fallback = overrides[0]["properties"][0]
            self.assertEqual(fallback, {"id": "displayName", "value": "Work name unavailable"})
        # Percentage bars are composition, not per-session identity fields.
        for identity in (34, 36):
            before = next(panel for panel in draft["panels"] if panel["id"] == identity)
            self.assertEqual(panels[identity]["fieldConfig"], before["fieldConfig"])
        self.assertNotIn("MUST NOT LEAK", json.dumps(result))
        self.assertEqual(draft, original)

    def test_readable_aliases_and_parent_context_are_presentation_only(self):
        source = snapshot()
        source["sessions"][0]["prompt"] = "PRIVATE PROMPT MUST NOT LEAK"
        source["sessions"][1]["reasoning"] = "PRIVATE REASONING MUST NOT LEAK"
        source["sessions"][1]["model"] = "CURRENT MODEL MUST NOT LEAK"
        before = copy.deepcopy(source)
        draft = template()
        detail = copy.deepcopy(draft["panels"][0]["panels"][0])
        detail["id"] = 41
        draft["panels"][0]["panels"].append(detail)
        original = copy.deepcopy(draft)
        result = render_dashboard(draft, source, "prometheus", {
            "version": 1, "projects": {"source": "CWO · source", "temporary": "CWO · temporary"},
            "sessions": {"parent": "Observability dashboard"},
        })
        fields = result["panels"][0]["panels"][0]["fieldConfig"]["overrides"]
        mappings = {f["matcher"]["options"]: f["properties"][0]["value"][0]["options"] for f in fields}
        self.assertEqual(mappings["Work"]["child"]["text"], "Review metrics")
        self.assertEqual(mappings["Session"]["child"]["text"], "Review metrics")
        self.assertEqual(mappings["Agent"]["child"]["text"], "Reviewer")
        self.assertEqual(mappings["Parent"]["parent"]["text"], "Observability dashboard")
        detail_fields = result["panels"][0]["panels"][1]["fieldConfig"]["overrides"]
        detail_mappings = {
            field["matcher"]["options"]: field["properties"][0]["value"][0]["options"]
            for field in detail_fields
        }
        self.assertEqual(
            detail_mappings["Work"]["child"]["text"],
            "Review metrics · Reviewer\n↳ Observability dashboard",
        )
        self.assertNotIn("MUST NOT LEAK", json.dumps(result))
        self.assertEqual(source, before)
        self.assertEqual(draft, original)
        self.assertEqual(result["panels"][0]["panels"][0]["targets"], draft["panels"][0]["panels"][0]["targets"])

    def test_project_and_work_aliases_render_as_named_custom_options(self):
        result = render_dashboard(template(), snapshot(), "prometheus")
        project, session = result["templating"]["list"]
        self.assertEqual(project["type"], "custom")
        self.assertEqual(session["type"], "custom")
        self.assertEqual(session["label"], "Work")
        self.assertEqual(session["hide"], 0)
        self.assertTrue(session["multi"])
        self.assertTrue(session["includeAll"])
        self.assertEqual(session["allValue"], ".+")
        self.assertNotIn("datasource", session)
        self.assertNotIn("definition", session)
        project_labels = [value["text"] for value in project["options"]]
        work_labels = [value["text"] for value in session["options"]]
        self.assertEqual(len(project_labels), len(set(project_labels)))
        self.assertEqual(work_labels, ["All", "Build dashboard", "Check installation", "Review metrics"])
        self.assertFalse(any("source" in label or "temporary" in label for label in project_labels))
        self.assertFalse(any(value in work_labels for value in ("parent", "child", "other")))

    def test_work_option_punctuation_round_trips_without_template_injection(self):
        source = snapshot()
        before = copy.deepcopy(source)
        draft = template()
        original = copy.deepcopy(draft)
        result = render_dashboard(draft, source, labels={
            "version": 1,
            "projects": {
                "source": r"CWO : source, R\D",
                "temporary": "Temporary workspace",
            },
            "sessions": {
                "parent": r"DECIDE : authorize, phase \ one",
                "child": r"Review\, metrics : follow-up",
            },
        })
        project, session = result["templating"]["list"]
        options = {value["value"]: value["text"] for value in session["options"]}
        self.assertEqual(options["parent"], r"DECIDE : authorize, phase \ one")
        self.assertEqual(options["child"], r"Review\, metrics : follow-up")
        for variable in (project, session):
            self.assertEqual(
                grafana_11_5_custom_options(variable["query"]),
                [
                    (option["text"], option["value"])
                    for option in variable["options"][1:]
                ],
            )
            self.assertNotIn(r"\:", variable["query"])
        self.assertEqual(source, before)
        self.assertEqual(draft, original)

    def test_unknown_parent_never_displays_opaque_identity(self):
        source = snapshot()
        source["sessions"][1]["parent_id"] = "unindexed-parent"
        names = presentation_names(source, validate_labels({"version": 1}))
        self.assertEqual(names["Work"]["child"], "Review metrics · Reviewer\n↳ Parent not indexed")
        rendered = render_dashboard(template(), source)
        overrides = rendered["panels"][0]["panels"][0]["fieldConfig"]["overrides"]
        parent = next(v for v in overrides if v["matcher"]["options"] == "Parent")
        self.assertEqual(parent["properties"][0]["value"][1]["options"]["result"]["text"], "Parent name unavailable")

    def test_same_millisecond_duplicate_titles_remain_distinct_and_stable(self):
        first = snapshot()["sessions"][0]
        second = {**first}
        first["session_id"] = "018e14dd-0000-7000-8000-000000000001"
        second["session_id"] = "018e14dd-0000-7000-8000-000000000002"
        source = {"version": 1, "sessions": [first, second]}
        names = presentation_names(source, validate_labels({"version": 1}))["Session"]
        self.assertEqual(len(set(names.values())), 2)
        self.assertIn("instance 2", names[second["session_id"]])
        self.assertNotIn(first["session_id"], " ".join(names.values()))
        source["sessions"].reverse()
        self.assertEqual(names, presentation_names(source, validate_labels({"version": 1}))["Session"])

    def test_rejects_wrong_uid_or_invalid_work_selector_contract(self):
        for mutate in (
            lambda d: d.update(uid="cwo-supervisor-observability-v1"),
            lambda d: d["templating"]["list"][1].update(type="query"),
            lambda d: d["templating"]["list"][1].update(label="Session"),
            lambda d: d["templating"]["list"][1].update(hide=2),
            lambda d: d["templating"]["list"][1].update(multi=False),
            lambda d: d["templating"]["list"][1].update(includeAll=False),
            lambda d: d["templating"]["list"][1].update(allValue=".*"),
            lambda d: d["templating"]["list"][1].update(
                datasource={"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
            ),
            lambda d: d["templating"]["list"][1].update(definition="sessions"),
        ):
            with self.subTest(mutate=mutate):
                draft = template()
                mutate(draft)
                with self.assertRaises(ValueError):
                    render_dashboard(draft, snapshot())

    def test_labels_reject_template_controls_ids_and_duplicate_project_names(self):
        for label in ("", " ", "bad\nname", "${project}", "$project", "[[project]]",
                      "Name 12345678-1234-1234-1234-123456789012", "a" * 257):
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_labels({"version": 1, "sessions": {"parent": label}})
        for invalid in ({"version": 2}, {"version": 1, "path": "/tmp"},
                        {"version": 1, "projects": {"a": "Same", "b": "same"}}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_labels(invalid)

    def test_protected_labels_reject_unsafe_modes_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "labels.json"
            path.write_text('{"version":1,"sessions":{"parent":"Dashboard"}}')
            path.chmod(0o600)
            self.assertEqual(load_labels(path)["sessions"]["parent"], "Dashboard")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_labels(path)
            path.chmod(0o600)
            path.write_text('{"version":1,"version":1}')
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_labels(path)

    def test_beta_output_never_overwrites_original_or_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dashboard.json"
            original = b'{"uid":"cwo-supervisor-observability-v1"}\n'
            path.write_bytes(original)
            with self.assertRaisesRegex(ValueError, "another dashboard"):
                write_beta_dashboard(path, template())
            self.assertEqual(path.read_bytes(), original)
            link = Path(folder) / "beta.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                write_beta_dashboard(link, template())
            self.assertEqual(path.read_bytes(), original)

    def test_output_updates_are_atomic_and_unchanged_content_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "beta.json"
            draft = template()
            self.assertTrue(write_beta_dashboard(path, draft))
            inode = path.stat().st_ino
            self.assertFalse(write_beta_dashboard(path, draft))
            self.assertEqual(path.stat().st_ino, inode)
            draft["title"] = "Beta"
            self.assertTrue(write_beta_dashboard(path, draft))
            self.assertEqual(json.loads(path.read_text())["title"], "Beta")

    def test_cli_renders_separate_dashboard_and_rejects_input_output_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, draft, output = (root / name for name in ("snapshot.json", "template.json", "beta.json"))
            source.write_text(json.dumps(snapshot()))
            source.chmod(0o600)
            draft.write_text(json.dumps(template()))
            command = [sys.executable, str(ROOT / "scripts/render_codex_beta_dashboard.py"),
                       "--template", str(draft), "--snapshot-file", str(source), "--output"]
            result = subprocess.run(command + [str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(output.read_text())["uid"], BETA_UID)
            before = draft.read_bytes()
            result = subprocess.run(command + [str(draft)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("separate from input files", result.stderr)
            self.assertEqual(draft.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
