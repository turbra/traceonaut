"""Check public guide links, executable block syntax, and source references."""

import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys
import unittest
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from traceonaut.codex_session_telemetry import (
    COMMAND_SOURCE_QUALIFICATION,
    COMPACTION_SOURCE_QUALIFICATION,
)

DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "references").glob("*.md"))]


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
        if link.fragment and target.suffix == ".md":
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
        self.assertIn('<h1 align="center">Traceonaut</h1>', readme)
        self.assertIn('alt="License: Apache-2.0"', readme)
        badge = ElementTree.parse(ROOT / "assets/license-apache-2.0.svg").getroot()
        self.assertEqual(badge.find("{http://www.w3.org/2000/svg}title").text, "License: Apache-2.0")
        self.assertIn('[Apache License 2.0](LICENSE)', readme)
        sections = ["Install", "Quick Start", "Documentation", "Commands at a Glance",
                    "Data Scope", "Related", "License"]
        positions = [readme.index("\n## " + section + "\n") for section in sections]
        self.assertEqual(positions, sorted(positions))
        # Canonical, unmodified text from https://www.apache.org/licenses/LICENSE-2.0.txt.
        self.assertEqual(hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(),
                         "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")

    def test_source_metadata_references_resolve_to_public_headings(self):
        for reference in (COMMAND_SOURCE_QUALIFICATION, COMPACTION_SOURCE_QUALIFICATION):
            with self.subTest(reference=reference):
                self.assert_reference(ROOT / "README.md", reference)

    def test_documented_shell_blocks_parse_without_execution(self):
        for doc in DOCS:
            blocks = re.findall(r"^```bash\n(.*?)^```\s*$", doc.read_text(), re.M | re.S)
            for number, block in enumerate(blocks, 1):
                with self.subTest(doc=doc.name, block=number):
                    result = subprocess.run(["bash", "-n"], input=block, text=True,
                                            capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
