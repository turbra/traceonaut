"""Check public guide links, executable block syntax, and source references."""

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

DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "references").glob("*.md"))]


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
            for reference in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", doc.read_text()):
                with self.subTest(doc=doc.name, reference=reference):
                    self.assert_reference(doc, reference)

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
