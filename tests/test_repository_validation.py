"""Publication checks use synthetic, independent repositories, including unborn HEAD."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from validate_repository import IndexSource, WorktreeSource, private_path, validate


class RepositoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.write("README.md", "# Synthetic repository\n")
        self.git("add", "--", "README.md")

    def git(self, *args, data=None):
        return subprocess.run(["git", "-C", str(self.root), *args], input=data,
                              capture_output=True, check=True).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def stage(self, name, text):
        path = self.write(name, text)
        self.git("add", "-f", "--", name)
        return path

    def errors(self):
        return validate(IndexSource(self.root))[0]

    def test_unborn_repository_cli_and_regular_executable_modes(self):
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.root), "rev-parse", "--verify", "HEAD"],
                                          capture_output=True).returncode, 0)
        self.stage("scripts/check.py", "import json\n")
        script = self.root / "scripts/validate_repository.py"
        shutil.copyfile(ROOT / "scripts/validate_repository.py", script)
        self.git("add", "scripts/validate_repository.py")
        self.git("update-index", "--chmod=+x", "scripts/check.py")
        result = subprocess.run([sys.executable, str(script), "--staged"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Source: Git index", result.stdout)

    def test_staged_bad_worktree_fixed_and_inverse(self):
        name = "examples/dashboard.json"
        self.stage(name, "broken json")
        self.write(name, "{}")
        self.assertTrue(any("JSONDecodeError" in e for e in self.errors()))
        self.git("add", "--", name)
        self.write(name, "broken again")
        self.assertEqual(self.errors(), [])

    def test_untracked_and_unstaged_docs_cannot_satisfy_links(self):
        self.stage("README.md", "[guide](references/guide.md)\n")
        self.write("references/guide.md", "# Guide\n")
        self.assertTrue(any("missing link" in e for e in self.errors()))
        self.git("add", "references/guide.md")
        self.assertEqual(self.errors(), [])
        self.git("rm", "--cached", "references/guide.md")
        self.assertTrue(any("missing link" in e for e in self.errors()))

    def test_worktree_link_repair_does_not_hide_staged_failure(self):
        self.stage("README.md", "[missing](missing.md)\n")
        self.write("README.md", "# Fixed only in worktree\n")
        self.assertTrue(any("missing link" in e for e in self.errors()))

    def test_mdx_and_pages_routes_are_checked_against_the_index(self):
        self.stage("website/docs-manifest.json", '["website/docs/home.mdx", "references/install.md"]')
        self.stage("website/docs/home.mdx", "---\nslug: /\n---\n[Install](/install/)\n")
        self.stage("references/install.md", "---\nslug: /install\n---\n")
        self.assertEqual(self.errors(), [])
        self.stage("website/docs/home.mdx", "---\nslug: /\n---\n[Invalid](/etc/hosts)\n")
        self.assertTrue(any("escapes repository" in e for e in self.errors()))
        self.stage("website/docs/home.mdx", "---\nslug: /\n---\n[Install](/install/)\n")
        self.stage("references/guide.mdx", "[Missing](missing.md)\n")
        self.assertTrue(any("missing link" in e for e in self.errors()))
        self.write("references/missing.md", "# Unstaged\n")
        self.assertTrue(any("missing link" in e for e in self.errors()))

    def test_untracked_module_and_package_marker_do_not_satisfy_imports(self):
        self.stage("scripts/main.py", "import helper\nfrom traceonaut.worker import run\n")
        self.write("scripts/helper.py", "")
        self.stage("scripts/traceonaut/worker.py", "def run(): pass\n")
        self.write("scripts/traceonaut/__init__.py", "")
        errors = self.errors()
        self.assertTrue(any("module/package helper" in e for e in errors), errors)
        self.assertTrue(any("module/package traceonaut.worker" in e for e in errors), errors)
        self.git("add", "scripts/helper.py", "scripts/traceonaut/__init__.py")
        self.assertEqual(self.errors(), [])

    def test_relative_import_uses_staged_module_and_package(self):
        self.stage("scripts/traceonaut/__init__.py", "")
        self.stage("scripts/traceonaut/main.py", "from .helper import run\n")
        self.write("scripts/traceonaut/helper.py", "def run(): pass\n")
        self.assertTrue(any("module/package traceonaut.helper" in e for e in self.errors()))
        self.git("add", "scripts/traceonaut/helper.py")
        self.assertEqual(self.errors(), [])
        self.stage("scripts/traceonaut/main.py", "from ..outside import run\n")
        self.assertTrue(any("escapes scripts package" in e for e in self.errors()))

    def test_runtime_controller_and_third_party_imports_rejected(self):
        self.stage("scripts/main.py", "import cwo_core\nimport requests\n")
        errors = self.errors()
        self.assertTrue(any("imports the CWO controller" in e for e in errors))
        self.assertTrue(any("module/package requests" in e for e in errors))

    def test_malformed_json_and_python_including_tests_are_reported(self):
        self.stage("examples/invalid.json", "[")
        self.stage("scripts/invalid.py", "def broken(:")
        self.stage("tests/invalid.py", "def broken(:")
        errors = self.errors()
        self.assertEqual(len(errors), 3, errors)
        self.assertTrue(any("JSONDecodeError" in e for e in errors))
        self.assertEqual(sum("SyntaxError" in e for e in errors), 2)

    def test_normalized_relative_links_anchors_and_external_urls(self):
        self.stage("references/guide.md", "[root](.././README.md#heading)\n[folder](../scripts/)\n"
                   "[repository](../)\n[external](https://example.invalid/guide)\n[anchor](#heading)\n")
        self.stage("scripts/helper.py", "")
        self.assertEqual(self.errors(), [])

    def test_staged_link_escape_rejected_even_when_external_file_exists(self):
        for target in ("../../outside.md", "/etc/hosts", "%2e%2e/%2e%2e/outside.md"):
            with self.subTest(target=target):
                self.stage("references/guide.md", f"[escape]({target})\n")
                self.assertTrue(any("link escapes repository" in e for e in self.errors()))

    def test_fixed_private_path_rules_are_case_sensitive_and_component_aware(self):
        denied = [".beads/tasks.json", "x/.migration/state", ".orchestration-audit/run.md",
                  ".orchestration-agents/worker", ".local/state", ".venv/bin/tool", ".dolt/state",
                  "scripts/__pycache__/cache.pyc", ".pytest_cache/state", ".env", "sub/.env.local",
                  ".beads-credential-key", "state/writer.lock", "sessions-snapshot.json",
                  "state/account-snapshot.json", "local.db", "a.sqlite", "b.sqlite3",
                  "a.db-wal", "a.sqlite-shm", "a.sqlite3-wal", "tls.pem", "tls.key", "tls.p12", "tls.pfx"]
        for name in denied:
            with self.subTest(name=name):
                self.assertTrue(private_path(name))
                self.stage(name, "synthetic")
                with self.assertRaisesRegex(ValueError, "prohibited private-state path"):
                    IndexSource(self.root)
                self.git("rm", "--cached", "--", name)
        for name in (".gitignore", "AGENTS.md", "schemas/public.schema.json", "references/.beads-guide.md",
                     "safe.sqlite.txt", "references/private-key-explanation.md", ".ENV", "locality/data.json"):
            with self.subTest(name=name):
                self.assertFalse(private_path(name))

    def test_symlink_rejected_without_following_target(self):
        (self.root / "link").symlink_to("absent")
        self.git("add", "link")
        with self.assertRaisesRegex(ValueError, "unsupported staged mode 120000"):
            IndexSource(self.root)

    def test_gitlink_rejected(self):
        oid = self.git("hash-object", "-w", "--stdin", data=b"synthetic").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{oid},submodule")
        with self.assertRaisesRegex(ValueError, "unsupported staged mode 160000"):
            IndexSource(self.root)

    def test_unmerged_index_rejected(self):
        oid = self.git("hash-object", "-w", "--stdin", data=b"synthetic").decode().strip()
        self.git("update-index", "--index-info", data=f"100644 {oid} 1\tconflict.md\n".encode())
        with self.assertRaisesRegex(ValueError, "unmerged index entry"):
            IndexSource(self.root)

    def test_filename_safe_plumbing_and_validation_do_not_write_git_or_source(self):
        self.stage("references/space and\nnewline.md", "# Synthetic\n")
        self.stage("examples/data.json", "{}\n")

        def snapshot():
            return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

        before = snapshot()
        self.assertEqual(self.errors(), [])
        self.assertEqual(snapshot(), before)

    def test_default_still_checks_worktree_without_git(self):
        shutil.rmtree(self.root / ".git")
        self.write("scripts/main.py", "import json\n")
        self.write("examples/data.json", "{}")
        self.assertEqual(validate(WorktreeSource(self.root)), ([], 1))
        self.write("examples/data.json", "invalid")
        self.assertTrue(validate(WorktreeSource(self.root))[0])


if __name__ == "__main__":
    unittest.main()
