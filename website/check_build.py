#!/usr/bin/env python3
"""Check the static Pages artifact without network access or extra packages."""

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit

BASE = "https://turbra.github.io/traceonaut/"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
def expected_pages(project_root=PROJECT_ROOT):
    """Resolve only explicitly declared public sources, never a repository crawl."""
    manifest = json.loads((project_root / "website/docs-manifest.json").read_text())
    if not isinstance(manifest, list) or not manifest or len(set(manifest)) != len(manifest):
        raise ValueError("Invalid public document manifest")
    pages = {"404.html"}
    for name in manifest:
        if not re.fullmatch(r"references/(?:[a-z-]+/)*[a-z-]+\.mdx?|website/docs/home\.mdx", name):
            raise ValueError(f"Unexpected public source: {name}")
        source = project_root / name
        if source.is_symlink() or not source.resolve().is_relative_to(project_root.resolve()):
            raise ValueError(f"Unsafe public source: {name}")
        text = source.read_text()
        front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        metadata = dict(re.findall(r"^(slug|title|description): (.+)$", front[1], re.M)) if front else {}
        if set(metadata) != {"slug", "title", "description"}:
            raise ValueError(f"Missing page metadata: {name}")
        slug = metadata["slug"]
        if not re.fullmatch(r"/(?:[a-z-]+(?:/[a-z-]+)*)?", slug):
            raise ValueError(f"Invalid page route: {name}")
        page = slug.lstrip("/") + "/index.html" if slug != "/" else "index.html"
        if page in pages:
            raise ValueError(f"Duplicate page route: {slug}")
        pages.add(page)
    return pages


PAGES = expected_pages()
PUBLIC_ASSETS = {
    "traceonaut-favicon.png", "traceonaut.png",
    "screenshots/work-overview.png", "screenshots/unified.png",
    "screenshots/all-sessions.png", "screenshots/cwo-dispatches.png",
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


def validate_build(root, expected=PAGES, public_assets=(), readme=None):
    root = Path(root)
    errors = []
    files = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
    pages = {name: Page(path.read_text()) for name, path in files.items() if name.endswith(".html")}
    if set(pages) != expected:
        errors.append(f"Unexpected page set: missing={expected - set(pages)}, extra={set(pages) - expected}")
    for name in public_assets:
        if name not in files:
            errors.append(f"Missing public asset: {name}")
        elif files[name].read_bytes() != (PROJECT_ROOT / "assets" / name).read_bytes():
            errors.append(f"Public asset differs from source: {name}")
    for name, path in files.items():
        if path.suffix in {".html", ".js", ".css", ".json", ".svg", ".xml"}:
            if str(PROJECT_ROOT) in path.read_text():
                errors.append(f"Build-machine path in artifact: {name}")
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"Symlink in artifact: {name}")
        if path.is_file() and name not in expected | set(public_assets) | {"sitemap.xml", ".nojekyll"} and not name.startswith("assets/"):
            errors.append(f"Unexpected artifact file: {name}")
    sources = [(name, name.removesuffix("index.html"), page.links) for name, page in pages.items()]
    if readme is not None:
        links = Page(readme).links
        links.extend(re.findall(r"\[[^\]\n]+\]\((https?://[^\s)]+)\)", readme))
        # Repository-relative links belong to GitHub, not to the Pages artifact.
        links = [link for link in links if urlsplit(link).netloc == urlsplit(BASE).netloc]
        sources.append(("README.md", "", links))
    for name, route, links in sources:
        for link in links:
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
    errors = validate_build(
        Path(__file__).parent / "build", public_assets=PUBLIC_ASSETS,
        readme=(PROJECT_ROOT / "README.md").read_text(),
    )
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Validated {len(PAGES)} HTML pages, README Pages links, anchors and declared public assets.")
