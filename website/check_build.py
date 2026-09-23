#!/usr/bin/env python3
"""Check the static Pages artifact without network access or extra packages."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

BASE = "https://turbra.github.io/traceonaut/"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGES = {
    "index.html", "404.html", "getting-started/index.html",
    "operations/index.html", "dashboards/beta/index.html",
    "dashboards/unified/index.html", "data-and-limits/index.html",
    "integrations/cwo/index.html", "integrations/terminal-export/index.html",
}


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = set()
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        # Canonical/alternate metadata is not a navigation or asset request.
        if tag == "link" and attrs.get("rel") not in ("stylesheet", "preload", "modulepreload", "icon"):
            return
        for key in ("href", "src"):
            if attrs.get(key):
                self.links.append(attrs[key])


def validate_build(root, expected=PAGES):
    root = Path(root)
    errors = []
    files = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
    pages = {name: Page(path.read_text()) for name, path in files.items() if name.endswith(".html")}
    if set(pages) != expected:
        errors.append(f"Unexpected page set: missing={expected - set(pages)}, extra={set(pages) - expected}")
    for name, path in files.items():
        if path.suffix in {".html", ".js", ".css", ".json", ".svg", ".xml"}:
            if str(PROJECT_ROOT) in path.read_text():
                errors.append(f"Build-machine path in artifact: {name}")
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"Symlink in artifact: {name}")
        if path.is_file() and name not in expected | {"sitemap.xml", ".nojekyll"} and not name.startswith("assets/"):
            errors.append(f"Unexpected artifact file: {name}")
    for name, page in pages.items():
        route = name.removesuffix("index.html")
        for link in page.links:
            url = urlsplit(urljoin(BASE + route, link))
            if url.scheme not in ("http", "https") or url.netloc != urlsplit(BASE).netloc:
                continue
            path = unquote(url.path)
            if not path.startswith("/traceonaut/"):
                errors.append(f"{name}: link escapes project base: {link}")
                continue
            target = path.removeprefix("/traceonaut/")
            if target.endswith("/") or not target:
                target += "index.html"
            if target not in files:
                errors.append(f"{name}: missing target: {link}")
            elif url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids:
                errors.append(f"{name}: missing anchor: {link}")
    return errors


if __name__ == "__main__":
    errors = validate_build(Path(__file__).parent / "build")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Validated {len(PAGES)} HTML pages, project-base links, anchors and local assets.")
