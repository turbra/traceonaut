#!/usr/bin/env python3
"""Render protected Codex session names into the native Grafana dashboard."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import threading
from typing import Any, Iterator
from uuid import UUID


DATASOURCE_PLACEHOLDER = "${DS_PROMETHEUS}"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
UUID_PATTERN = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
KINDS = {"session": "Codex session", "subagent": "Subagent", "internal": "Codex internal"}
REQUIRED_SESSION_FIELDS = {
    "session_id",
    "project_id",
    "title",
    "project_name",
    "agent_name",
    "kind",
    "parent_id",
}


def walk_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def _mapping(names: dict[str, str], fallback: str) -> list[dict[str, Any]]:
    return [
        {"type": "value", "options": {key: {"text": name} for key, name in names.items()}},
        {"type": "regex", "options": {"pattern": r"^.+$", "result": {"text": fallback}}},
    ]


def _custom_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(":", "\\:")


def _display_name(value: str, fallback: str) -> str:
    if re.search(r"\$(?:\w|\{)|\[\[", value):
        return fallback
    return value


def _uuid7_started(value: str) -> str:
    try:
        identity = UUID(value)
    except (ValueError, AttributeError):
        return "date unavailable"
    if identity.version != 7:
        return "date unavailable"
    milliseconds = identity.int >> 80
    try:
        started = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        return started.strftime("%Y-%m-%d %H:%M:%S.") + f"{milliseconds % 1000:03d} UTC"
    except (OverflowError, OSError, ValueError):
        return "date unavailable"


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"snapshot {field} must be a string")
    value = " ".join(value.strip().split())
    if len(value) > 512:
        raise ValueError(f"snapshot {field} is too long")
    return value


def _identity(value: Any, *, field: str) -> str:
    value = _text(value, field=field)
    if IDENTITY_PATTERN.fullmatch(value) is None:
        raise ValueError(f"snapshot {field} is invalid")
    return value


def validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("unsupported session snapshot")
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("snapshot sessions must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sessions:
        if not isinstance(item, dict) or not REQUIRED_SESSION_FIELDS.issubset(item):
            raise ValueError("snapshot session fields are invalid")
        session_id = _identity(item["session_id"], field="session_id")
        if session_id in seen:
            raise ValueError("snapshot session IDs must be unique")
        seen.add(session_id)
        project_id = _identity(item["project_id"], field="project_id")
        kind = _text(item["kind"], field="kind")
        if kind not in KINDS:
            raise ValueError("snapshot session kind is invalid")
        parent_id = item["parent_id"]
        if parent_id is not None:
            parent_id = _identity(parent_id, field="parent_id")
        normalized.append(
            {
                "session_id": session_id,
                "project_id": project_id,
                "title": _text(item["title"], field="title"),
                "project_name": _text(item["project_name"], field="project_name"),
                "agent_name": _text(item["agent_name"], field="agent_name"),
                "kind": kind,
                "parent_id": parent_id,
            }
        )
    return {"version": 1, "sessions": normalized}


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_snapshot_bytes(path: Path) -> bytes:
    path = Path(os.path.abspath(path))
    for parent in reversed(path.parents):
        try:
            metadata = parent.lstat()
        except OSError:
            raise ValueError("snapshot path invalid") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("snapshot path invalid")
        if metadata.st_uid not in (0, os.geteuid()):
            raise ValueError("snapshot path invalid")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not (
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        ):
            raise ValueError("snapshot path invalid")
    try:
        parent_metadata = path.parent.lstat()
    except OSError:
        raise ValueError("snapshot path invalid") from None
    if (
        parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise ValueError("snapshot parent must be owner-only")
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("snapshot unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("snapshot must be an owned mode 0600 regular file")
        content = bytearray()
        while len(content) <= MAX_SNAPSHOT_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_SNAPSHOT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot too large")
        return bytes(content)
    finally:
        os.close(descriptor)


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_snapshot_bytes(path), object_pairs_hook=_object_without_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("snapshot JSON invalid") from None
    return validate_snapshot(value)


def _project_name(session: dict[str, Any]) -> str:
    name = session["project_name"]
    if not name or re.fullmatch(UUID_PATTERN, name, flags=re.IGNORECASE):
        return "Project name unavailable"
    return _display_name(name, "Project name contains unsupported template syntax")


def _session_name(session: dict[str, Any], project_name: str) -> str:
    title = session["title"]
    if title and re.fullmatch(UUID_PATTERN, title, flags=re.IGNORECASE) is None:
        return _display_name(title, "Session title contains unsupported template syntax")
    return f"{project_name} · {KINDS[session['kind']]} · {_uuid7_started(session['session_id'])}"


def _agent_name(session: dict[str, Any]) -> str:
    if session["agent_name"]:
        return _display_name(
            session["agent_name"], "Agent name contains unsupported template syntax"
        )
    return {"session": "Codex", "subagent": "Subagent", "internal": "Codex internal"}[
        session["kind"]
    ]


def _named_variable(variable: dict[str, Any], names: dict[str, str]) -> None:
    ordered = sorted(names.items(), key=lambda item: (item[1].casefold(), item[0]))
    variable.update(
        type="custom",
        query=", ".join(
            _custom_escape(name) + " : " + _custom_escape(identity)
            for identity, name in ordered
        ),
        hide=0 if names else 2,
        options=[{"text": "All", "value": "$__all", "selected": True}]
        + [
            {"text": name, "value": identity, "selected": False}
            for identity, name in ordered
        ],
        current={"text": "All", "value": "$__all", "selected": True},
    )
    for field in ("datasource", "definition", "refresh", "regex", "sort"):
        variable.pop(field, None)


def render_dashboard(
    template: dict[str, Any], snapshot: dict[str, Any], datasource_uid: str | None = None
) -> dict[str, Any]:
    """Apply snapshot labels to presentation without putting names in metrics."""
    snapshot = validate_snapshot(snapshot)
    dashboard = copy.deepcopy(template)
    projects: dict[str, str] = {}
    sessions: dict[str, str] = {}
    agents: dict[str, str] = {}
    base_session_names: dict[str, str] = {}
    for session in snapshot["sessions"]:
        project_name = _project_name(session)
        existing = projects.setdefault(session["project_id"], project_name)
        if existing != project_name:
            raise ValueError("snapshot project names conflict")
        base_session_names[session["session_id"]] = _session_name(session, project_name)
        agents[session["session_id"]] = _agent_name(session)
    name_counts: dict[str, int] = {}
    for name in base_session_names.values():
        name_counts[name] = name_counts.get(name, 0) + 1
    for session in snapshot["sessions"]:
        identity = session["session_id"]
        name = base_session_names[identity]
        if name_counts[name] > 1:
            name = f"{name} · {_uuid7_started(identity)}"
        sessions[identity] = name

    mappings = {
        "Session": _mapping(sessions, "Session name unavailable"),
        "Project": _mapping(projects, "Project name unavailable"),
        "Agent": _mapping(agents, "Agent name unavailable"),
    }
    for variable in dashboard.get("templating", {}).get("list", []):
        if variable.get("name") == "project":
            _named_variable(variable, projects)
        elif variable.get("name") == "session":
            _named_variable(variable, sessions)
            variable["label"] = "Session"
    for panel in walk_panels(dashboard.get("panels", [])):
        if panel.get("type") == "bargauge":
            panel.setdefault("fieldConfig", {}).setdefault("overrides", []).extend(
                {
                    "matcher": {"id": "byName", "options": identity},
                    "properties": [{"id": "displayName", "value": name}],
                }
                for identity, name in sessions.items()
            )
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            field = override.get("matcher", {}).get("options")
            if field not in mappings:
                continue
            for prop in override.get("properties", []):
                if prop.get("id") == "mappings":
                    prop["value"] = copy.deepcopy(mappings[field])

    if datasource_uid is not None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", datasource_uid) is None:
            raise ValueError("invalid datasource UID")

        def bind(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            return datasource_uid if value == DATASOURCE_PLACEHOLDER else value

        dashboard = bind(dashboard)
        dashboard.pop("__inputs", None)
    return dashboard


def _validated_output_path(path: Path) -> Path:
    normalized = Path(os.path.abspath(path))
    for parent in normalized.parents:
        try:
            metadata = parent.lstat()
        except OSError:
            raise ValueError("dashboard output path invalid") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("dashboard output path invalid")
    try:
        metadata = normalized.lstat()
    except FileNotFoundError:
        return normalized
    except OSError:
        raise ValueError("dashboard output path invalid") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("dashboard output path invalid")
    return normalized


def _matches_existing_output(path: Path, payload: bytes) -> bool:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("dashboard output path invalid") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("dashboard output path invalid")
        content = bytearray()
        maximum = len(payload) + 1
        while len(content) < maximum:
            chunk = os.read(descriptor, maximum - len(content))
            if not chunk:
                break
            content.extend(chunk)
        return bytes(content) == payload
    except OSError:
        raise ValueError("dashboard output path invalid") from None
    finally:
        os.close(descriptor)


def write_dashboard(path: Path, dashboard: dict[str, Any]) -> bool:
    payload = (json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = _validated_output_path(path)
    if _matches_existing_output(path, payload):
        return False
    descriptor, temporary = tempfile.mkstemp(
        prefix=".codex-sessions-dashboard-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--datasource-uid")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--watch-seconds", type=float, help="refresh local name mappings every 1 to 60 seconds"
    )
    args = parser.parse_args(argv)
    if args.watch_seconds is not None and not 1 <= args.watch_seconds <= 60:
        parser.error("--watch-seconds must be between 1 and 60")
    stop = threading.Event()
    if args.watch_seconds is not None:
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
        signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    try:
        while True:
            template = json.loads(args.template.read_text(encoding="utf-8"))
            snapshot = load_snapshot(args.snapshot_file)
            rendered = render_dashboard(template, snapshot, args.datasource_uid)
            changed = write_dashboard(args.output, rendered)
            if changed or args.watch_seconds is None:
                print(
                    json.dumps(
                        {
                            "status": "rendered",
                            "changed": changed,
                            "named_projects": len(
                                {item["project_id"] for item in snapshot["sessions"]}
                            ),
                            "named_sessions": len(snapshot["sessions"]),
                        }
                    ),
                    flush=True,
                )
            if args.watch_seconds is None or stop.wait(args.watch_seconds):
                break
    except (json.JSONDecodeError, OSError, ValueError, KeyError, TypeError):
        print("Dashboard render unavailable: check template, protected snapshot, and output path.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
