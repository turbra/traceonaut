from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from traceonaut.observability_presentation import (  # noqa: E402
    empty_presentation_registry,
    write_presentation_registry,
)
from render_observability_dashboard import render_dashboard, walk_panels, write_dashboard  # noqa: E402


class WorkDashboardRendererTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((ROOT / "examples/observability/cwo-overview.json").read_text())
        self.current_template = copy.deepcopy(self.template)
        # Exercise retained custom controller-template support separately from
        # the shipped session/review dashboard.
        controller = json.loads((ROOT / "tests/fixtures/cwo-controller-queries.json").read_text())
        self.template["panels"] += controller["panels"]
        self.template["templating"] = controller["templating"]
        self.project = str(uuid4())
        self.dispatch = str(uuid4())
        self.registry = {
            "version": 1,
            "projects": {self.project: {"name": "Release readiness"}},
            "dispatches": {self.dispatch: {"task_name": "Check installation on Linux", "agent_name": "Compatibility reviewer"}},
        }

    def session_template(self):
        template = copy.deepcopy(self.current_template)
        template["templating"] = {"list": [
            {
                "name": name, "label": label, "type": "query", "hide": 0,
                "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
                "query": {"query": query, "refId": "StandardVariableQuery"},
                "definition": query, "refresh": 2, "sort": 1,
                "includeAll": True, "allValue": ".*", "multi": True,
                "options": [], "current": {"text": "All", "value": "$__all"},
            }
            for name, label, query in (
                ("project", "Project", "label_values(cwo_codex_session_info, project_id)"),
                ("session", "Session", 'label_values(cwo_codex_session_info{project_id=~"$project"}, session_id)'),
            )
        ]}
        return template

    def session_snapshot(self, *, title="Improve the search page", project_name="Example app"):
        return {"version": 1, "sessions": [{
            "session_id": self.dispatch, "project_id": self.project,
            "title": title, "project_name": project_name, "agent_name": "",
            "kind": "session", "parent_id": None,
        }]}

    def test_current_selectors_use_snapshot_names_and_keep_metric_identities(self):
        template = self.session_template()
        before = copy.deepcopy(template)
        result = render_dashboard(template, self.registry, session_snapshot=self.session_snapshot())
        self.assertEqual(template, before)
        variables = {variable["name"]: variable for variable in result["templating"]["list"]}
        for name, label, identity, text in (
            ("project", "Project", self.project, "Example app"),
            ("session", "Session", self.dispatch, "Improve the search page"),
        ):
            with self.subTest(variable=name):
                variable = variables[name]
                self.assertEqual(variable["label"], label)
                self.assertEqual(variable["type"], "custom")
                self.assertEqual(variable["hide"], 0)
                self.assertEqual(variable["options"][1], {"text": text, "value": identity, "selected": False})
                self.assertEqual(variable["current"]["value"], "$__all")
                self.assertTrue(variable["includeAll"])
                self.assertTrue(variable["multi"])
                self.assertEqual(variable["allValue"], ".*")
                self.assertNotIn("Release readiness", variable["query"])
        self.assertEqual(
            [(panel["id"], panel.get("targets")) for panel in walk_panels(result["panels"])],
            [(panel["id"], panel.get("targets")) for panel in walk_panels(before["panels"])],
        )
        for panel_id in (305, 401):
            panel = next(panel for panel in result["panels"] if panel["id"] == panel_id)
            self.assertIn("Improve the search page", json.dumps(panel["fieldConfig"]))

    def test_current_selectors_remain_visible_query_fallbacks_without_names(self):
        for snapshot in (None, {"version": 1, "sessions": []}):
            with self.subTest(snapshot=snapshot):
                template = self.session_template()
                result = render_dashboard(template, self.registry, session_snapshot=snapshot)
                self.assertEqual(result["templating"], template["templating"])
                for variable in result["templating"]["list"]:
                    self.assertEqual(variable["hide"], 0)
                    self.assertEqual(variable["type"], "query")
                    self.assertEqual(variable["current"]["value"], "$__all")
                bound = render_dashboard(
                    template, self.registry, session_snapshot=snapshot,
                    datasource_uid="existing-prometheus",
                )
                self.assertNotIn("${DS_PROMETHEUS}", json.dumps(bound))

    def test_current_selector_names_escape_separators_and_reject_template_expansion(self):
        for text, expected in (
            ("Build, test: release", "Build\\, test\\: release"),
            ("Task ${__field.name}", None),
            ("Task $project", None),
            ("Task [[project]]", None),
        ):
            with self.subTest(text=text):
                result = render_dashboard(
                    self.session_template(), self.registry,
                    session_snapshot=self.session_snapshot(title=text, project_name=text),
                )
                for variable in result["templating"]["list"]:
                    identity = self.project if variable["name"] == "project" else self.dispatch
                    self.assertEqual(variable["options"][1]["value"], identity)
                    if expected is not None:
                        self.assertEqual(variable["query"], expected + " : " + identity)
                        self.assertEqual(variable["options"][1]["text"], text)
                    else:
                        self.assertIn("contains unsupported template syntax", variable["options"][1]["text"])
                        self.assertNotIn(text, variable["query"])

    def test_current_selectors_preserve_snapshot_validation(self):
        snapshot = self.session_snapshot()
        snapshot["sessions"][0]["session_id"] = "not a safe identity"
        with self.assertRaisesRegex(ValueError, "session_id is invalid"):
            render_dashboard(self.session_template(), self.registry, session_snapshot=snapshot)

    def test_readable_names_do_not_change_queries_or_accounting(self):
        before = copy.deepcopy(self.template)
        result = render_dashboard(self.template, self.registry, datasource_uid="existing-prometheus")
        self.assertEqual(result["uid"], "cwo-dispatch-observability-v1")
        self.assertEqual(self.template, before)
        original = [(p["id"], t["expr"]) for p in walk_panels(before["panels"]) for t in p.get("targets", [])]
        rendered = [(p["id"], t["expr"]) for p in walk_panels(result["panels"]) for t in p.get("targets", [])]
        self.assertEqual(rendered, original)
        self.assertNotIn("__inputs", result)
        self.assertNotIn("${DS_PROMETHEUS}", json.dumps(result))
        work = next(p for p in walk_panels(result["panels"]) if p["title"] == "Work and results")
        overrides = {o["matcher"]["options"]: o for o in work["fieldConfig"]["overrides"]}
        for field, label in (("Task", "Check installation on Linux"), ("Worker", "Compatibility reviewer")):
            mapping = next(p["value"] for p in overrides[field]["properties"] if p["id"] == "mappings")
            self.assertEqual(mapping[0]["options"][self.dispatch]["text"], label)
        variables = {v["name"]: v for v in result["templating"]["list"]}
        self.assertEqual(variables["dispatch"]["label"], "Observed task")
        self.assertEqual(variables["project"]["options"][1]["text"], "Release readiness")
        # Technical identities stay available only in collapsed diagnostics.
        row = next(p for p in result["panels"] if p["id"] == 90)
        self.assertTrue(row["collapsed"])
        for panel in walk_panels(result["panels"]):
            if panel["type"] == "bargauge" and any(t["id"] == "rowsToFields" for t in panel.get("transformations", [])):
                labels = panel["fieldConfig"]["overrides"]
                named = next(o for o in labels if o["matcher"]["options"] == self.dispatch)
                self.assertEqual(named["properties"], [
                    {"id": "displayName", "value": "Check installation on Linux"}
                ])

    def test_missing_names_get_explanatory_fallbacks_and_hidden_id_selectors(self):
        result = render_dashboard(self.template, empty_presentation_registry())
        self.assertTrue(all(v["hide"] == 2 for v in result["templating"]["list"]))
        work = next(p for p in walk_panels(result["panels"]) if p["title"] == "Work and results")
        text = json.dumps(work["fieldConfig"])
        self.assertIn("Task name not provided", text)
        self.assertIn("Worker name not provided", text)
        self.assertNotIn(self.dispatch, text)

    def test_session_names_preserve_dispatch_queries_and_selectors(self):
        snapshot={'version':1,'sessions':[{'session_id':self.dispatch,'project_id':self.project,
            'title':'Improve the search page','project_name':'Example app','agent_name':'',
            'kind':'session','parent_id':None}]}
        before=render_dashboard(self.template,self.registry)
        result=render_dashboard(self.template,self.registry,session_snapshot=snapshot)
        self.assertEqual(result['templating'],before['templating'])
        self.assertEqual([(p['id'],p.get('targets')) for p in result['panels']],
                         [(p['id'],p.get('targets')) for p in before['panels']])
        native=next(p for p in result['panels'] if p['id']==305)
        self.assertIn('Improve the search page',json.dumps(native['fieldConfig']))
        self.assertIn('Example app',json.dumps(native['fieldConfig']))
        review = next(p for p in result['panels'] if p['id'] == 401)
        source = next(o for o in review['fieldConfig']['overrides'] if o['matcher']['options'] == 'Source session')
        mapping = next(p['value'] for p in source['properties'] if p['id'] == 'mappings')
        self.assertEqual(mapping[0]['options'][self.dispatch]['text'], 'Improve the search page')
        link = next(p['value'][0]['url'] for p in source['properties'] if p['id'] == 'links')
        self.assertIn('var-session=${__value.raw}', link)
        self.assertIn('Requested effort', json.dumps(review['transformations']))
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);root.chmod(0o700)
            source=root/'snapshot.json';source.write_text(json.dumps(snapshot));source.chmod(0o600)
            output=root/'dashboard.json'
            args=[sys.executable,str(ROOT/'scripts/render_observability_dashboard.py'),
                '--template',str(ROOT/'examples/observability/cwo-overview.json'),
                '--session-snapshot-file',str(source),'--output',str(output)]
            completed=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
            self.assertIn('Improve the search page',output.read_text())
            source.chmod(0o644)
            self.assertNotEqual(subprocess.run(args,capture_output=True).returncode,0)

    def test_bar_names_cannot_expand_grafana_variables_into_identities(self):
        for name in ("Task ${__field.name}", "Task $project", "Task [[project]]"):
            with self.subTest(name=name):
                self.registry["dispatches"][self.dispatch]["task_name"] = name
                rendered = render_dashboard(self.template, self.registry)
                for panel in walk_panels(rendered["panels"]):
                    if panel["type"] != "bargauge" or not any(t["id"] == "rowsToFields" for t in panel.get("transformations", [])):
                        continue
                    override = next(o for o in panel["fieldConfig"]["overrides"]
                                    if o["matcher"]["options"] == self.dispatch)
                    label = override["properties"][0]["value"]
                    self.assertEqual(label, "Task name contains unsupported template syntax")
                    self.assertNotIn(self.dispatch, label)

    def test_names_with_variable_separators_cannot_change_selected_identity(self):
        self.registry["projects"][self.project]["name"] = "Build, test: release"
        result = render_dashboard(self.template, self.registry)
        variable = result["templating"]["list"][0]
        self.assertEqual(variable["query"], "Build\\, test\\: release : " + self.project)
        self.assertEqual(variable["options"][1]["value"], self.project)
        self.assertEqual(variable["options"][1]["text"], "Build, test: release")
        with self.assertRaises(ValueError):
            render_dashboard(self.template, self.registry, datasource_uid="invalid/uid")

    def test_atomic_output_preserves_unchanged_file_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "dashboard.json"
            result = render_dashboard(self.template, self.registry)
            self.assertTrue(write_dashboard(target, result))
            stamp = target.stat().st_mtime_ns
            self.assertFalse(write_dashboard(target, result))
            self.assertEqual(stamp, target.stat().st_mtime_ns)
            link = root / "linked.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                write_dashboard(link, result)
            self.assertEqual(json.loads(target.read_text()), result)

    def test_output_refuses_symlinked_ancestor(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            real_parent = root / "real"
            real_parent.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "output path invalid"):
                write_dashboard(linked_parent / "dashboard.json", self.template)
            self.assertFalse((real_parent / "dashboard.json").exists())

    def test_output_refuses_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "dashboard.json"
            os.mkfifo(output, mode=0o600)
            probe = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from render_observability_dashboard import write_dashboard
try:
    write_dashboard(Path(sys.argv[2]), {"title": "probe"})
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        probe,
                        str(ROOT / "scripts"),
                        str(output),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except subprocess.TimeoutExpired:
                self.fail("dashboard output validation blocked on a FIFO")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output.is_fifo())

    def test_watch_updates_task_names_without_restarting_collector(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.chmod(0o700)
            registry = root / "presentation.json"
            write_presentation_registry(registry, self.registry)
            output = root / "dashboard.json"
            template = root / "template.json"
            template.write_text(json.dumps(self.template))
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts/render_observability_dashboard.py"),
                 "--template", str(template),
                 "--presentation-file", str(registry), "--output", str(output),
                 "--watch-seconds", "1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            try:
                def wait_for(label):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll())
                        if output.exists() and label in output.read_text():
                            return
                        time.sleep(0.05)
                    self.fail("presentation watcher did not publish the expected label")
                wait_for("Check installation on Linux")
                self.registry["dispatches"][self.dispatch]["task_name"] = "Check the macOS installer"
                write_presentation_registry(registry, self.registry)
                wait_for("Check the macOS installer")
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
