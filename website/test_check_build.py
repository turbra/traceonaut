from pathlib import Path
import json
import tempfile
import unittest

from check_build import EDIT_BASE, EDIT_LINKS, PROJECT_ROOT, PUBLIC_ASSETS, expected_pages, validate_build


class BuildChecks(unittest.TestCase):
    def check(self, html, extras=None, public_assets=(), readme=None, edit_links=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text(html)
            for name, content in (extras or {}).items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
            return validate_build(root, {"index.html"}, public_assets=public_assets, readme=readme, edit_links=edit_links)

    def test_each_document_has_its_exact_source_edit_link(self):
        for target in EDIT_LINKS.values():
            with self.subTest(target=target):
                html = f'<a class="theme-edit-this-page" href="{target}">Edit this page</a>'
                self.assertEqual(self.check(html, edit_links={"index.html": target}), [])
                source = target.removeprefix(EDIT_BASE)
                self.assertTrue((PROJECT_ROOT / source).is_file())
        self.assertEqual(EDIT_LINKS["install/index.html"], EDIT_BASE + "references/install.md")

    def test_missing_duplicate_or_wrong_source_edit_link_is_rejected(self):
        expected = {"index.html": EDIT_BASE + "website/docs/home.mdx"}
        valid = f'<a class="theme-edit-this-page" href="{expected["index.html"]}">Edit</a>'
        invalid_targets = [
            "https://github.com/turbra/traceonaut/edit/references/install.md",
            EDIT_BASE + "../references/install.md",
            EDIT_BASE + "references/install.md",
            EDIT_BASE + "missing.md",
        ]
        invalid = ["", valid * 2, *[
            f'<a class="theme-edit-this-page" href="{target}">Edit</a>'
            for target in invalid_targets
        ]]
        for html in invalid:
            with self.subTest(html=html):
                self.assertIn("Invalid source edit link", self.check(html, edit_links=expected)[0])

    def test_project_links_assets_fragments_and_external_links(self):
        self.assertEqual(self.check(
            '<h1 id="intro">Intro</h1><a href="#intro">Intro</a>'
            '<a href="https://github.com/turbra/traceonaut">Source</a>'
            '<script src="/traceonaut/assets/app.js"></script>',
            {"assets/app.js": ""}), [])

    def test_missing_asset(self):
        self.assertIn("missing target", self.check('<img src="assets/missing.svg">')[0])

    def test_declared_public_asset_matches_source(self):
        for name in PUBLIC_ASSETS:
            with self.subTest(name=name):
                image = (PROJECT_ROOT / "assets" / name).read_bytes()
                self.assertEqual(self.check(f'<img src="/traceonaut/{name}">',
                                            {name: image}, {name}), [])
                self.assertIn("Missing public asset", self.check("", public_assets={name})[0])
                self.assertIn("differs from source", self.check("", {name: b"changed"}, {name})[0])

    def test_unlisted_root_asset_is_rejected(self):
        self.assertIn("Unexpected artifact file", self.check("", {"other.png": b"image"})[0])

    def test_checks_asset_links_not_canonical_metadata(self):
        self.assertEqual(self.check('<link rel="canonical" href="/traceonaut/404.html/">'), [])
        self.assertTrue(self.check('<link rel="stylesheet" href="assets/missing.css">'))
        self.assertIn("missing target", self.check('<link rel="icon" href="missing.png">')[0])

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

    def test_manifest_requires_public_paths_metadata_and_unique_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "website").mkdir()
            (root / "website/redirects.json").write_text("[]")
            (root / "references").mkdir()
            manifest = root / "website/docs-manifest.json"
            guide = root / "references/guide.md"
            guide.write_text("---\nslug: /guide\ntitle: Guide\ndescription: A guide.\n---\n")
            manifest.write_text(json.dumps(["references/guide.md"]))
            self.assertEqual(expected_pages(root), {"404.html", "guide/index.html"})
            for names in ([], ["../private.md"], ["references/guide.md"] * 2):
                manifest.write_text(json.dumps(names))
                with self.assertRaises(ValueError):
                    expected_pages(root)
            manifest.write_text(json.dumps(["references/guide.md"]))
            guide.write_text("---\nslug: /guide\n---\n")
            with self.assertRaisesRegex(ValueError, "metadata"):
                expected_pages(root)
            guide.write_text("---\nslug: /../private\ntitle: Guide\ndescription: A guide.\n---\n")
            with self.assertRaisesRegex(ValueError, "route"):
                expected_pages(root)

    def test_redirects_require_unique_routes_and_document_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "website").mkdir()
            (root / "references").mkdir()
            (root / "website/docs-manifest.json").write_text('["references/guide.md"]')
            (root / "references/guide.md").write_text("---\nslug: /guide\ntitle: Guide\ndescription: A guide.\n---\n")
            manifest = root / "website/redirects.json"
            valid = {"from": "/old/", "to": "/guide/", "fragments": {"#legacy": "/guide/"}}
            manifest.write_text(json.dumps([valid]))
            self.assertEqual(expected_pages(root), {"404.html", "guide/index.html", "old/index.html"})
            invalid = [
                [valid, valid], [{"from": "/guide/", "to": "/guide/"}],
                [{"from": "/old/", "to": "/missing/"}],
                [{"from": "/../private/", "to": "/guide/"}],
                [{**valid, "fragments": {"#legacy": "/missing/"}}],
                [{**valid, "to": "https://example.org/"}],
            ]
            for rules in invalid:
                with self.subTest(rules=rules):
                    manifest.write_text(json.dumps(rules))
                    with self.assertRaises(ValueError):
                        expected_pages(root)

    def test_built_redirect_canonical_must_match_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "old").mkdir()
            (root / "guide").mkdir()
            (root / "guide/index.html").write_text("")
            alias = root / "old/index.html"
            rule = {"from": "/old/", "to": "/guide/"}
            expected = {"old/index.html", "guide/index.html"}
            alias.write_text('<link rel="canonical" href="/traceonaut/guide/">')
            self.assertEqual(validate_build(root, expected, redirects=[rule]), [])
            alias.write_text('<link rel="canonical" href="https://example.org/">')
            self.assertTrue(any("Invalid redirect" in e for e in validate_build(root, expected, redirects=[rule])))


if __name__ == "__main__":
    unittest.main()
