#!/usr/bin/env python3
"""Build a pinned source bundle; never change services or private configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = "examples/observability/"
COMPONENTS = {
    # Existing account/exporter launchers share one configured scripts directory.
    "sessions": ("scripts/collect_codex_sessions.py", "scripts/collect_codex_account.py"),
    "account": ("scripts/collect_codex_sessions.py", "scripts/collect_codex_account.py"),
    "stable": (
        "scripts/render_codex_sessions_dashboard.py",
        TEMPLATES + "codex-all-sessions.json",
    ),
    "beta": (
        "scripts/render_codex_sessions_dashboard.py",
        "scripts/render_codex_beta_dashboard.py",
        TEMPLATES + "codex-work-overview-beta.json",
    ),
    "dispatch": (
        "scripts/run_observed_codex.py",
        "scripts/export_dispatch_observability.py",
        "scripts/export_terminal_observations.py",
        "scripts/render_observability_dashboard.py",
        "scripts/render_codex_sessions_dashboard.py",
        TEMPLATES + "cwo-overview.json",
    ),
}


def build_release(component: str, output_dir: Path) -> Path:
    paths = ["LICENSE", *COMPONENTS[component]]
    if component in ("sessions", "account", "dispatch"):
        paths.extend(str(path.relative_to(ROOT)) for path in sorted(
            (ROOT / "scripts/traceonaut").glob("*.py")
        ))
    contents = {name: (ROOT / name).read_bytes() for name in paths}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    output_dir = output_dir.absolute()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = output_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("release directory must be owner-controlled and not a symlink")
    release = output_dir / digest
    if release.exists() or release.is_symlink():
        if release.is_symlink() or not release.is_dir():
            raise ValueError("existing release is not a directory")
        expected = {**contents, "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode()}
        actual = {str(path.relative_to(release)) for path in release.rglob("*") if path.is_file()}
        if actual != set(expected) or any(path.is_symlink() for path in release.rglob("*")):
            raise ValueError("existing release file set changed")
        if any((release / name).read_bytes() != data for name, data in expected.items()):
            raise ValueError("existing release content changed")
        return release
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=output_dir))
    try:
        for name, data in contents.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        staging.rename(release)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", required=True, choices=COMPONENTS)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(build_release(args.component, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
