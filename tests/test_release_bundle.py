"""Exercise independently runnable releases and immutable-file rejection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_release import COMPONENTS, build_release


class ReleaseBundleTests(unittest.TestCase):
    def test_dashboard_assets_match_their_titles_and_release_components(self):
        templates = {
            "stable": ("codex-all-sessions.json", "All Sessions"),
            "beta": ("codex-work-overview-beta.json", "Work Overview"),
            "dispatch": ("cwo-overview.json", "CWO Overview"),
        }
        self.assertEqual(set(COMPONENTS), {"sessions", "account", *templates})
        self.assertEqual(
            {p.name for p in (ROOT / "examples/observability").glob("*.json")},
            {filename for filename, _ in templates.values()},
        )
        for component, (filename, title) in templates.items():
            with self.subTest(component=component):
                path = ROOT / "examples" / "observability" / filename
                self.assertEqual(json.loads(path.read_text())["title"], title)
                self.assertIn(f"examples/observability/{filename}", COMPONENTS[component])

    def test_cwo_overview_renders_from_the_standalone_dispatch_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = build_release("dispatch", root / "releases")
            templates = release / "examples/observability"
            self.assertEqual({p.name for p in templates.glob("*.json")}, {"cwo-overview.json"})
            snapshot = root / "sessions.json"
            snapshot.write_text(json.dumps({"version": 1, "sessions": []}))
            snapshot.chmod(0o600)
            output = root / "cwo-overview.json"
            result = subprocess.run([
                sys.executable, "-I", "-B", "-c",
                "import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); "
                "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                str(release / "scripts"),
                str(release / "scripts/render_observability_dashboard.py"),
                "--template", str(templates / "cwo-overview.json"),
                "--session-snapshot-file", str(snapshot),
                "--datasource-uid", "synthetic-prometheus", "--output", str(output),
            ], cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            dashboard = json.loads(output.read_text())
            self.assertEqual(dashboard["title"], "CWO Overview")
            self.assertEqual(dashboard["uid"], "cwo-dispatch-observability-v1")
            self.assertNotIn("${DS_PROMETHEUS}", output.read_text())
            self.assertGreater(len(dashboard["panels"]), 0)

    def test_beta_renders_nonempty_metadata_from_its_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "sessions.json"
            snapshot.write_text(json.dumps({"version": 1, "sessions": [{
                "session_id": str(uuid4()), "project_id": str(uuid4()),
                "title": "Synthetic work", "project_name": "Example project",
                "agent_name": "", "kind": "session", "parent_id": None,
            }]}))
            snapshot.chmod(0o600)
            self.assertEqual(build_release("sessions", root / "releases"),
                             build_release("account", root / "releases"))
            templates = {
                "beta": "codex-work-overview-beta.json",
            }
            for component, template_name in templates.items():
                with self.subTest(component=component):
                    release = build_release(component, root / "releases")
                    output = root / f"{component}.json"
                    result = subprocess.run([
                        sys.executable, "-I", "-B", "-c",
                        "import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); "
                        "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                        str(release / "scripts"),
                        str(release / f"scripts/render_codex_{component}_dashboard.py"),
                        "--template", str(release / "examples" / "observability" / template_name),
                        "--snapshot-file", str(snapshot), "--datasource-uid", "synthetic-prometheus",
                        "--output", str(output),
                    ], cwd=root, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    dashboard = json.loads(output.read_text())
                    self.assertEqual(dashboard["uid"], f"cwo-codex-{component}")
                    self.assertIn("Synthetic work", output.read_text())
                    self.assertIn("synthetic-prometheus", output.read_text())
                    self.assertNotIn("${DS_PROMETHEUS}", output.read_text())
                    self.assertNotIn("__inputs", dashboard)
                    self.assertGreater(len(dashboard["panels"]), 0)

    def test_every_component_runs_without_the_checkout_or_cwo(self):
        entrypoints = {
            "sessions": ["collect_codex_sessions.py", "collect_codex_account.py"],
            "account": ["collect_codex_sessions.py", "collect_codex_account.py"],
            "stable": ["render_codex_sessions_dashboard.py"],
            "beta": ["render_codex_beta_dashboard.py"],
            "dispatch": ["run_observed_codex.py", "export_dispatch_observability.py",
                         "export_terminal_observations.py", "render_observability_dashboard.py"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for component, scripts in entrypoints.items():
                with self.subTest(component=component):
                    release = build_release(component, root / "releases")
                    self.assertEqual(build_release(component, root / "releases"), release)
                    manifest = json.loads((release / "manifest.json").read_text())
                    self.assertIn("LICENSE", manifest)
                    self.assertEqual((release / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
                    for name, digest in manifest.items():
                        self.assertEqual(hashlib.sha256((release / name).read_bytes()).hexdigest(), digest)
                        self.assertNotIn("cwo_core", name)
                    if component in ("sessions", "account", "dispatch"):
                        self.assertIn("scripts/traceonaut/__init__.py", manifest)
                    for script in scripts:
                        # -I removes ambient PYTHONPATH and cwd imports; explicitly add
                        # only this staged bundle, matching a direct script invocation.
                        result = subprocess.run(
                            [sys.executable, "-I", "-B", "-c",
                             "import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); "
                             "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                             str(release / "scripts"), str(release / "scripts" / script), "--help"],
                            cwd=root, capture_output=True, text=True, timeout=15,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("usage:", result.stdout)

    def test_existing_release_corruption_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            releases = Path(temporary)
            release = build_release("beta", releases)
            template = release / "examples/observability/codex-work-overview-beta.json"
            template.write_text("changed")
            with self.assertRaisesRegex(ValueError, "content changed"):
                build_release("beta", releases)
            self.assertEqual(template.read_text(), "changed")

    def test_release_directory_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = build_release("account", root / "releases")
            renamed = release.with_name("retained")
            release.rename(renamed)
            release.symlink_to(renamed, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "not a directory"):
                build_release("account", root / "releases")


if __name__ == "__main__":
    unittest.main()
