from pathlib import Path
import tempfile
import unittest

from check_build import PROJECT_ROOT, validate_build


class BuildChecks(unittest.TestCase):
    def check(self, html, extras=None, public_assets=(), readme=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text(html)
            for name, content in (extras or {}).items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
            return validate_build(root, {"index.html"}, public_assets=public_assets, readme=readme)

    def test_project_links_assets_fragments_and_external_links(self):
        self.assertEqual(self.check(
            '<h1 id="intro">Intro</h1><a href="#intro">Intro</a>'
            '<a href="https://github.com/turbra/traceonaut">Source</a>'
            '<script src="/traceonaut/assets/app.js"></script>',
            {"assets/app.js": ""}), [])

    def test_missing_asset(self):
        self.assertIn("missing target", self.check('<img src="assets/missing.svg">')[0])

    def test_declared_public_asset_matches_source(self):
        image = (PROJECT_ROOT / "assets/traceonaut.png").read_bytes()
        self.assertEqual(self.check('<img src="/traceonaut/traceonaut.png">',
                                    {"traceonaut.png": image}, {"traceonaut.png"}), [])
        self.assertIn("Missing public asset", self.check("", public_assets={"traceonaut.png"})[0])
        self.assertIn("differs from source", self.check("", {"traceonaut.png": b"changed"},
                                                        {"traceonaut.png"})[0])

    def test_unlisted_root_asset_is_rejected(self):
        self.assertIn("Unexpected artifact file", self.check("", {"other.png": b"image"})[0])

    def test_checks_asset_links_not_canonical_metadata(self):
        self.assertEqual(self.check('<link rel="canonical" href="/traceonaut/404.html/">'), [])
        self.assertTrue(self.check('<link rel="stylesheet" href="assets/missing.css">'))

    def test_missing_anchor(self):
        self.assertIn("missing anchor", self.check('<a href="#missing">Go</a>')[0])

    def test_readme_html_and_markdown_links_resolve_to_built_pages(self):
        readme = (
            '<a href="https://turbra.github.io/traceonaut/">Documentation</a>\n'
            '[Dashboards](https://turbra.github.io/traceonaut/#dashboards)\n'
            '[License](LICENSE)\n<img src="assets/traceonaut.png">\n'
            '[Source](https://github.com/turbra/traceonaut)'
        )
        self.assertEqual(self.check('<h2 id="dashboards">Dashboards</h2>', readme=readme), [])

    def test_readme_missing_page_is_rejected(self):
        errors = self.check("", readme='[Install](https://turbra.github.io/traceonaut/install/)')
        self.assertEqual(len(errors), 1)
        self.assertIn("README.md: missing target", errors[0])

    def test_readme_missing_anchor_is_rejected(self):
        for readme in (
            '<a href="https://turbra.github.io/traceonaut/#missing">Go</a>',
            '[Go](https://turbra.github.io/traceonaut/#missing)',
        ):
            with self.subTest(readme=readme):
                errors = self.check("", readme=readme)
                self.assertEqual(len(errors), 1)
                self.assertIn("README.md: missing anchor", errors[0])

    def test_readme_base_path_escape_is_rejected(self):
        errors = self.check("", readme='<a href="https://turbra.github.io/install/">Install</a>')
        self.assertIn("README.md: link escapes project base", errors[0])

    def test_base_path_escape(self):
        for link in ("/getting-started/", "../", "/traceonaut/%2e%2e/private"):
            with self.subTest(link=link):
                self.assertTrue(self.check(f'<a href="{link}">Go</a>'))

    def test_extra_page_is_rejected(self):
        self.assertIn("Unexpected page set", self.check("", {"private.html": ""})[0])

    def test_private_artifact_is_rejected(self):
        self.assertIn("Unexpected artifact file", self.check("", {".beads/state.db": ""})[0])

    def test_build_machine_paths_are_rejected(self):
        errors = self.check("", {"assets/app.js": f'const path = "{PROJECT_ROOT}/website/config.js";'})
        self.assertIn("Build-machine path", errors[0])

    def test_empty_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(validate_build(root))

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text("")
            (root / "assets").mkdir()
            (root / "assets/link.html").symlink_to(root / "index.html")
            self.assertTrue(any("Symlink" in error for error in validate_build(root, {"index.html"})))


if __name__ == "__main__":
    unittest.main()
