"""Owned, non-aliasing paths for optional local telemetry sources."""

import os
from pathlib import Path
import stat


def checked_path(path, *, missing=False, private=False):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.geteuid()):
            raise ValueError("telemetry path must be owned and contain no symlinks")
        if info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and
                                       info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise ValueError("telemetry path must be owner-controlled")
        if part == path:
            if info.st_uid != os.geteuid():
                raise ValueError("telemetry path must belong to the collector user")
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise ValueError("telemetry path must be a regular file or directory")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("telemetry files must not have hardlink aliases")
            if private and stat.S_IMODE(info.st_mode) != (0o700 if stat.S_ISDIR(info.st_mode) else 0o600):
                raise ValueError("collector output must be owner-only")
    return path


def validate_outputs(sources, state, snapshots):
    sources = [checked_path(p, missing=True) for p in sources]
    state = checked_path(state, missing=True, private=True)
    snapshots = [checked_path(p, missing=True, private=True) for p in snapshots]
    if len(set(snapshots)) != len(snapshots):
        raise ValueError("each source needs a separate snapshot file")
    for output in [state, *snapshots]:
        for source in sources:
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError("collector output must be separate from source homes")
    reserved = {"sessions.sqlite3", "writer.lock", "cwo-sessions.sqlite3", "cwo-writer.lock",
                "bob.sqlite3", "bob-writer.lock"}
    for path in snapshots:
        if path == state or state.is_relative_to(path):
            raise ValueError("snapshot must be separate from the state directory")
        if path.parent == state and any(path.name == name + suffix for name in reserved
                                       for suffix in ('', '-wal', '-shm', '-journal')):
            raise ValueError("snapshot must not replace collector state")
    return state
