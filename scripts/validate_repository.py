#!/usr/bin/env python3
"""Validate portable Traceonaut assets without running unrelated CWO checks."""

from __future__ import annotations

import ast
import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_COMPONENTS = {
    ".beads", ".migration", ".orchestration-audit", ".orchestration-agents",
    ".local", ".venv", ".dolt", "__pycache__", ".pytest_cache",
}
PRIVATE_BASENAMES = {
    ".beads-credential-key", "writer.lock", "sessions-snapshot.json", "account-snapshot.json",
}
PRIVATE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx")


def private_path(name: str) -> bool:
    parts = name.split("/")
    base = parts[-1]
    database = base.removesuffix("-wal").removesuffix("-shm")
    return bool(
        PRIVATE_COMPONENTS.intersection(parts)
        or base in PRIVATE_BASENAMES or base == ".env" or base.startswith(".env.")
        or base.endswith(PRIVATE_SUFFIXES)
        or database.endswith((".db", ".sqlite", ".sqlite3"))
    )


class WorktreeSource:
    """The original working-tree checks, with no Git dependency."""

    staged = False

    def __init__(self, root: Path):
        self.root = root
        paths = [root / "README.md", *sorted((root / "references").rglob("*.md")),
                 *sorted((root / "references").rglob("*.mdx")),
                 *sorted((root / "scripts").rglob("*.py")),
                 *sorted((root / "examples").rglob("*.json")),
                 *sorted((root / "schemas").glob("*.json"))]
        paths.extend(root / name for name in ("website/docs/home.mdx", "website/docs-manifest.json")
                     if (root / name).is_file())
        self.names = {path.relative_to(root).as_posix() for path in paths}

    def read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()


class IndexSource:
    """A read-only snapshot of the selected repository's index, even without HEAD."""

    staged = True

    def __init__(self, root: Path):
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1",
               "GIT_NO_REPLACE_OBJECTS": "1"}

        def git(*args: str, data: bytes | None = None) -> bytes:
            result = subprocess.run(["git", "-C", str(root), *args], input=data,
                                    capture_output=True, env=env, check=False)
            if result.returncode:
                raise ValueError("cannot read Git index or objects")
            return result.stdout

        entries: dict[str, str] = {}
        for entry in git("ls-files", "--stage", "--full-name", "-z").split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8")
            if stage != "0":
                raise ValueError(f"{name!r}: unmerged index entry")
            if mode not in ("100644", "100755"):
                raise ValueError(f"{name!r}: unsupported staged mode {mode}")
            if name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")):
                raise ValueError(f"{name!r}: invalid staged path")
            if private_path(name):
                raise ValueError(f"{name!r}: prohibited private-state path")
            entries[name] = oid
        # Read each distinct object once, by object ID, never by worktree path.
        ids = list(dict.fromkeys(entries.values()))
        raw = git("cat-file", "--batch", data="".join(oid + "\n" for oid in ids).encode())
        blobs: dict[str, bytes] = {}
        offset = 0
        for oid in ids:
            end = raw.index(b"\n", offset)
            actual, kind, size = raw[offset:end].decode("ascii").split()
            if actual != oid or kind != "blob":
                raise ValueError("staged object is not a blob")
            start, length = end + 1, int(size)
            blobs[oid] = raw[start:start + length]
            offset = start + length + 1
            if raw[offset - 1:offset] != b"\n" or len(blobs[oid]) != length:
                raise ValueError("incomplete staged object")
        self.contents = {name: blobs[oid] for name, oid in entries.items()}
        self.names = set(entries)

    def read(self, name: str) -> str:
        return self.contents[name].decode("utf-8")

    def exists(self, name: str) -> bool:
        return (name in self.names or name == "." and bool(self.names)
                or any(p.startswith(name.rstrip("/") + "/") for p in self.names))


def module_exists(source, name: str) -> bool:
    parts = name.split(".")
    base = "scripts/" + "/".join(parts)
    return (
        (base + ".py" in source.names or base + "/__init__.py" in source.names)
        and all("scripts/" + "/".join(parts[:i]) + "/__init__.py" in source.names
                for i in range(1, len(parts)))
    )


def validate(source) -> tuple[list[str], int]:
    errors: list[str] = []
    json_files = sorted(name for name in source.names if name.endswith(".json"))
    for name in json_files:
        try:
            json.loads(source.read(name))
        except (ValueError, OSError, KeyError) as exc:
            errors.append(f"{name!r}: {type(exc).__name__}")

    for name in sorted(n for n in source.names if n.endswith(".py")):
        try:
            tree = ast.parse(source.read(name), filename=name)
        except (SyntaxError, ValueError, OSError, KeyError) as exc:
            errors.append(f"{name!r}: {type(exc).__name__}")
            continue
        if not name.startswith("scripts/"):
            continue  # Tests need syntax checks, not the runtime import policy.
        for node in ast.walk(tree):
            names: list[str] = []
            relative = False
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            elif isinstance(node, ast.ImportFrom) and source.staged:
                relative = True
                package = name.split("/")[1:-1]
                if node.level > len(package):
                    errors.append(f"{name!r}: relative import escapes scripts package")
                    continue
                package = package[:len(package) - node.level + 1]
                names = [".".join(package + ([node.module] if node.module else []))]
            for module in names:
                root = module.split(".")[0]
                if root in ("cwo_core", "run_native_pool_live_canaries"):
                    errors.append(f"{name!r} imports the CWO controller")
                elif source.staged:
                    if (relative or root not in sys.stdlib_module_names) and not module_exists(source, module):
                        errors.append(f"{name!r}: missing staged runtime module/package {module}")
                elif root not in sys.stdlib_module_names and not (
                    root == "traceonaut" or source.exists(f"scripts/{root}.py")
                ):
                    errors.append(f"{name!r}: non-stdlib runtime import {root}")

    # Homepage links target published routes. Other documents use source-relative
    # links. Resolve routes from the same source/index snapshot as the links.
    page_routes = set()
    if "website/docs-manifest.json" in source.names:
        try:
            documents = json.loads(source.read("website/docs-manifest.json"))
            if not isinstance(documents, list) or not all(isinstance(p, str) for p in documents):
                raise ValueError("invalid manifest")
            for document in documents:
                if document not in source.names:
                    errors.append(f"website/docs-manifest.json: missing source {document!r}")
                    continue
                route = re.search(r"^slug: (/(?:[a-z-]+(?:/[a-z-]+)*)?)$", source.read(document), re.M)
                if route:
                    page_routes.add(route[1].rstrip("/") + "/")
        except (ValueError, OSError, KeyError):
            errors.append("website/docs-manifest.json: invalid page manifest")

    for name in sorted(n for n in source.names if n.endswith((".md", ".mdx"))):
        try:
            content = source.read(name)
        except (ValueError, OSError, KeyError) as exc:
            errors.append(f"{name!r}: {type(exc).__name__}")
            continue
        for link in re.findall(r"\]\(([^)]+)\)", content):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link) or link.startswith("#"):
                continue
            target = link.split("#", 1)[0]
            if not target:
                continue
            if name == "website/docs/home.mdx" and target in page_routes:
                continue
            joined = posixpath.join(posixpath.dirname(name), unquote(target) if source.staged else target)
            normalized = posixpath.normpath(joined)
            if source.staged and (normalized.startswith("/") or normalized == ".." or normalized.startswith("../")):
                errors.append(f"{name!r}: link escapes repository {target!r}")
            elif not source.exists(normalized):
                errors.append(f"{name!r}: missing link {target!r}")
    return errors, len(json_files)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="validate exact Git index bytes and publication paths, read-only")
    args = parser.parse_args(argv)
    try:
        source = IndexSource(ROOT) if args.staged else WorktreeSource(ROOT)
        errors, count = validate(source)
    except (ValueError, OSError) as exc:
        print(f"Cannot validate repository: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Validated {count} JSON assets, runtime imports and documentation links."
          + (" Source: Git index; publication path/mode checks passed." if args.staged else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
