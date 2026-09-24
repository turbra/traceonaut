"""Check public guide links, executable block syntax, and source references."""

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.codex_session_telemetry import (
    COMMAND_SOURCE_QUALIFICATION,
    COMPACTION_SOURCE_QUALIFICATION,
)

DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "references").rglob("*.md")),
        *sorted((ROOT / "references").rglob("*.mdx"))]


class HTMLReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src") and value is not None:
                self.references.append(value)


def heading_ids(markdown):
    """Heading anchors for the plain Markdown headings used by these guides."""
    anchors = set()
    counts = {}
    prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", markdown, flags=re.M | re.S)
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", prose, re.M):
        slug = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


class DocumentationTests(unittest.TestCase):
    def assert_reference(self, origin, reference):
        link = urlsplit(reference)
        if link.scheme or link.netloc:
            return
        target = (origin.parent / unquote(link.path)).resolve() if link.path else origin
        self.assertTrue(target.is_relative_to(ROOT), reference)
        self.assertTrue(target.exists(), f"{origin.name}: {reference}")
        if link.fragment and target.suffix in (".md", ".mdx"):
            self.assertIn(unquote(link.fragment), heading_ids(target.read_text()), reference)

    def test_relative_guide_links_and_headings_exist(self):
        for doc in DOCS:
            text = doc.read_text()
            html = HTMLReferences()
            html.feed(text)
            references = re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text) + html.references
            for reference in references:
                with self.subTest(doc=doc.name, reference=reference):
                    self.assert_reference(doc, reference)

    def test_html_navigation_and_images_are_checked(self):
        html = HTMLReferences()
        html.feed('<a href="#quick-start"><img src="missing-doc-image.svg"></a>')
        self.assertEqual(html.references, ["#quick-start", "missing-doc-image.svg"])
        with self.assertRaises(AssertionError):
            self.assert_reference(ROOT / "README.md", html.references[1])

    def test_readme_layout_and_apache_license(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn('<h1 align="center"><a href="https://turbra.github.io/traceonaut/"><img src="assets/traceonaut.png" '
                      'alt="Traceonaut: Explore every run" width="840"></a></h1>', readme)
        self.assertIn('<a href="https://www.apache.org/licenses/LICENSE-2.0">'
                      '<img src="https://img.shields.io/badge/License-Apache--2.0-2C7A7B?style=flat-square" '
                      'alt="License: Apache-2.0"></a>', readme)
        self.assertEqual(re.findall(r"^## (.+)$", readme, re.M),
                         ["Install", "Quick Start", "Dashboards at a Glance", "Documentation"])
        # Canonical, unmodified text from https://www.apache.org/licenses/LICENSE-2.0.txt.
        self.assertEqual(hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(),
                         "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")

    def test_source_metadata_references_resolve_to_public_headings(self):
        for reference in (COMMAND_SOURCE_QUALIFICATION, COMPACTION_SOURCE_QUALIFICATION):
            with self.subTest(reference=reference):
                self.assert_reference(ROOT / "README.md", reference)

    def test_documented_shell_blocks_parse_without_execution(self):
        for doc in [*DOCS, ROOT / "website/docs/home.mdx"]:
            blocks = re.findall(r"^```bash\n(.*?)^```\s*$", doc.read_text(), re.M | re.S)
            for number, block in enumerate(blocks, 1):
                with self.subTest(doc=doc.name, block=number):
                    result = subprocess.run(["bash", "-n"], input=block, text=True,
                                            capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_quick_start_blocks_match_readme_and_home(self):
        guide = (ROOT / "references/getting-started.mdx").read_text()
        for name in ("setup-paths", "credential-create", "run-collector", "prometheus-scrape", "render-beta"):
            pattern = r"<!-- " + name + r" -->(?:\s*\*/})?\s*```\w+\n(.*?)\n```"
            expected = re.search(pattern, guide, re.S)[1]
            for file in ("README.md", "website/docs/home.mdx"):
                with self.subTest(marker=name, file=file):
                    self.assertEqual(re.search(pattern, (ROOT / file).read_text(), re.S)[1], expected)

    def test_network_scrape_examples_change_only_target_and_token_path(self):
        guide = (ROOT / "references/getting-started.mdx").read_text()
        def block(marker):
            return re.search(r"<!-- " + marker + r" -->(?:\s*\*/})?\s*```yaml\n(.*?)\n```", guide, re.S)[1]
        local = block("prometheus-scrape")
        remote = local.replace("127.0.0.1:9464", "192.0.2.10:9464").replace(
            "/absolute/path/to/traceonaut/metrics.token", "/etc/prometheus/traceonaut/metrics.token"
        )
        self.assertEqual(block("prometheus-scrape-remote"), remote)
        self.assertEqual(block("prometheus-scrape-container"), remote)

    def test_all_guides_have_explicit_pages_and_metadata(self):
        manifest = json.loads((ROOT / "website/docs-manifest.json").read_text())
        guides = {p.relative_to(ROOT).as_posix() for p in DOCS if p.is_relative_to(ROOT / "references")}
        self.assertEqual(set(manifest), guides | {"website/docs/home.mdx"})
        for file in guides:
            text = (ROOT / file).read_text()
            self.assertRegex(text, r"(?m)^description: .+$")
            title = re.search(r"(?m)^title: (.+)$", text)[1]
            self.assertEqual(re.search(r"(?m)^# (.+)$", text)[1], title)

    def test_scripts_and_dashboard_inventory_match_entry_points(self):
        scripts = (ROOT / "references/reference/scripts.md").read_text()
        for path in (ROOT / "scripts").glob("*.py"):
            if '__name__ == "__main__"' in path.read_text():
                self.assertIn(f"`{path.name}`", scripts)
        self.assertIn("`--poll-seconds 5`", scripts)
        for name in ("collect_codex_sessions.py", "collect_codex_account.py", "export_dispatch_observability.py"):
            row = next(line for line in scripts.splitlines() if line.startswith(f"| `{name}`"))
            self.assertIn("`--once`", row)
        inventory = (ROOT / "references/reference/dashboards.md").read_text()
        for path in (ROOT / "examples/observability").glob("*.json"):
            data = json.loads(path.read_text())
            if "panels" in data:
                for value in (data["uid"], data["title"], path.name):
                    self.assertIn(value, inventory)


if __name__ == "__main__":
    unittest.main()
